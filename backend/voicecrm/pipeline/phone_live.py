"""Phone line: calls to the VoiceCRM line are answered, recorded on separate channels and sent through the pipeline.

The call, as this service drives it (Azure Communication Services Call Automation):

  caller dials the line               a browser (VoIP) today; a real phone once a Teams Phone service number is attached
  Event Grid  IncomingCall            -> answer the call                                  (must happen inside the 30 s ring)
  callback    CallConnected           -> play the recording notice
  callback    PlayCompleted           -> start an UNMIXED recording, then ring the seller (a Teams user or a browser console)
  callback    AddParticipantSucceeded -> the seller and the caller talk
  callback    AddParticipantFailed /  -> no seller: tell the caller to leave a message and keep recording
              ring timeout
  callback    ParticipantsUpdated     -> when either side leaves, end the call for everyone
  callback    CallDisconnected        -> the call is over
  Event Grid  RecordingFileStatusUpdated (a few minutes later)
                                      -> download the WAV + metadata, give caller and seller their own channel, transcribe
                                         each channel separately, and run the same pipeline as every other source

Because every participant has a channel of their own, the transcript knows who said what without guessing. The recording is
only started after the notice has played, and the conversation records whether the caller heard it.

Everything the service remembers about a call is in memory plus a small JSON file (so a restart does not lose the line
identity or the recent calls, and a recording that arrives after a restart is still matched to its call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field

from ..adapters.sources import phone as wire
from ..config import Settings
from ..domain.base import VModel
from ..domain.conversation import ConsentInfo, ConversationMeta, Participant, utcnow
from ..domain.enums import Channel, ConversationStatus, Direction, SpeakerRole
from ..ports.telephony import TelephonyError, TelephonyPort, Target
from ..ports.transcription import NO_SPEECH_MESSAGE
from .models import IngestInput
from .service import PipelineService

logger = logging.getLogger(__name__)

PhoneCallState = Literal["ringing", "connecting", "notice", "calling_seller", "in_call", "leaving_message", "ended",
                         "processing", "ingested", "failed", "missed"]
_TERMINAL = {"ingested", "failed", "missed"}
_LIVE_STATES = {"ringing", "connecting", "notice", "calling_seller", "in_call", "leaving_message"}

RECENT_KEPT = 25
TIMELINE_MAX = 40
SEEN_KEPT = 300
PROFILES_KEPT = 500
CONSOLE_TTL = timedelta(seconds=45)              # a seller console must heartbeat inside this window to be rung
NOTICE_WAIT_SECONDS = 45
RECORDING_WAIT_SECONDS = 15 * 60                 # how long to wait for the recording after the call before reporting it missing
VOICEMAIL_PROMPT = "Nobody is available right now. Please leave your message after the tone, then hang up."
CUSTOMER_ID, SELLER_ID = "cust1", "seller"       # participant ids: the transcript labels each channel with these
_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


# ---- public models (the API contract) ---------------------------------------------------------------------------------
class PhoneCallEvent(VModel):
    at: datetime
    label: str


class PhoneCall(VModel):
    call_id: str
    state: PhoneCallState = "ringing"
    caller: str                                   # E.164 number for phone callers, display name for browser (voip) callers
    caller_kind: Literal["phone", "voip"] = "voip"
    caller_name: str | None = None
    seller: str | None = None
    started_at: datetime
    connected_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    notice: Literal["pending", "played", "skipped", "failed"] = "pending"
    recording: Literal["none", "active", "ready", "failed"] = "none"
    conversation_id: str | None = None
    error: str | None = None
    timeline: list[PhoneCallEvent] = Field(default_factory=list)


class PhoneSeller(VModel):
    mode: Literal["teams", "console", "none"] = "none"
    name: str | None = None
    online: bool = False


class PhoneStatus(VModel):
    mode: str = "off"                             # off | acs
    configured: bool = False
    problem: str | None = None
    number: str | None = None
    line_identity: str | None = None
    public_url: str | None = None
    ingress_listening: bool = False
    seller: PhoneSeller = Field(default_factory=PhoneSeller)
    notice_enabled: bool = False
    active: list[PhoneCall] = Field(default_factory=list)
    recent: list[PhoneCall] = Field(default_factory=list)
    calls: int = 0
    ingested: int = 0
    failed: int = 0
    missed: int = 0
    last_event_at: datetime | None = None
    last_error: str | None = None


class CallerProfile(VModel):
    name: str | None = Field(default=None, max_length=120)
    org: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)


class PhoneTokenRequest(VModel):
    role: Literal["caller", "seller"]
    display_name: str | None = Field(default=None, max_length=120)
    identity: str | None = Field(default=None, max_length=300)
    profile: CallerProfile | None = None


class PhoneToken(VModel):
    identity: str
    token: str
    expires_on: datetime
    display_name: str
    line_identity: str
    number: str | None = None


@dataclass(frozen=True)
class DirectoryUser:
    id: str
    name: str | None = None
    email: str | None = None


@dataclass
class _Seller:
    kind: Literal["teams", "console"]
    target: Target
    name: str | None
    email: str | None
    ids: list[str]                                # raw ids the recording metadata may use for this person


@dataclass
class _Console:
    name: str | None
    email: str | None
    seen: datetime


@dataclass
class _Live:
    pub: PhoneCall
    caller: wire.Party
    context: str = ""                             # incomingCallContext: single use, never logged or saved
    profile: CallerProfile | None = None
    conn: str | None = None
    server_call_id: str | None = None
    recording_id: str | None = None
    seller: _Seller | None = None
    invitation: str | None = None
    caller_seen: bool = False
    seller_joined: bool = False
    hang_up_sent: bool = False
    timers: list[asyncio.Task[None]] = field(default_factory=list)      # step timers: notice, ring, voicemail
    max_timer: asyncio.Task[None] | None = None                         # the maximum call length
    watchdog: asyncio.Task[None] | None = None                          # waiting for the recording after the call
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _safe(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:300]


class PhoneLine:
    def __init__(self, service: PipelineService | None, telephony: TelephonyPort | None, *, settings: Settings,
                 state_file: Path | None = None, problem: str | None = None,
                 directory: Callable[[str], Awaitable[DirectoryUser | None]] | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """``service=None`` gives a read-only line (checks and status only). ``sleep`` is injectable for the call timers."""
        self.service, self.tel, self.settings = service, telephony, settings
        self._sleep = sleep
        self._state_file = state_file
        self._directory = directory
        self._http = http_client
        self._problem = problem
        self._lock = asyncio.Lock()
        self._calls: dict[str, _Live] = {}
        self._order: list[str] = []
        self._consoles: dict[str, _Console] = {}
        self._profiles: dict[str, CallerProfile] = {}
        self._dir_cache: dict[str, DirectoryUser | None] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._seen_calls: list[str] = []
        self._seen_recordings: list[str] = []
        self._line: str | None = settings.phone_line_identity.strip() or None
        self._line_lock = asyncio.Lock()
        self._counters = {"calls": 0, "ingested": 0, "failed": 0, "missed": 0}
        self._last_event_at: datetime | None = None
        self._last_error: str | None = None
        self.ingress_listening = False
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._line = self._line or raw.get("line_identity") or None
            self._seen_calls = list(raw.get("seen_calls") or [])[-SEEN_KEPT:]
            self._seen_recordings = list(raw.get("seen_recordings") or [])[-SEEN_KEPT:]
            for item in raw.get("calls") or []:
                pub = PhoneCall.model_validate(item["pub"])
                private = item.get("private") or {}
                live = _Live(pub=pub, caller=wire.Party.model_validate(private.get("caller") or {}),
                             server_call_id=private.get("server_call_id"),
                             profile=CallerProfile.model_validate(private["profile"]) if private.get("profile") else None)
                if private.get("seller"):
                    s = private["seller"]
                    live.seller = _Seller(kind=s["kind"], target=Target(**s["target"]), name=s.get("name"),
                                          email=s.get("email"), ids=list(s.get("ids") or []))
                if pub.state in _LIVE_STATES:                  # the process that owned this call is gone
                    pub.state = "ended" if pub.recording == "active" else "missed"
                    pub.error = pub.error or "the app restarted during the call"
                elif pub.state == "processing":               # so is the pipeline run it was waiting for
                    pub.state, pub.error = "failed", "the app restarted while the recording was being processed"
                self._calls[pub.call_id] = live
                self._order.append(pub.call_id)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("could not read %s; starting with no remembered calls", self._state_file, exc_info=True)

    def _save(self) -> None:
        if self._state_file is None:
            return
        calls = []
        for call_id in self._order[-RECENT_KEPT:]:
            live = self._calls.get(call_id)
            if live is None:
                continue
            seller = live.seller
            calls.append({"pub": live.pub.model_dump(mode="json"), "private": {
                "caller": live.caller.model_dump(), "server_call_id": live.server_call_id,
                "profile": live.profile.model_dump() if live.profile else None,
                "seller": None if seller is None else {
                    "kind": seller.kind, "name": seller.name, "email": seller.email, "ids": seller.ids,
                    "target": {"kind": seller.target.kind, "id": seller.target.id,
                               "display_name": seller.target.display_name}}}})
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "line_identity": self._line, "seen_calls": self._seen_calls[-SEEN_KEPT:],
                "seen_recordings": self._seen_recordings[-SEEN_KEPT:], "calls": calls}), encoding="utf-8")
        except OSError:
            logger.debug("could not write %s", self._state_file, exc_info=True)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Makes sure the line identity exists; a failure is reported in the status, not raised."""
        if self.tel is None:
            return
        try:
            await self.ensure_line()
        except Exception as exc:
            self._last_error = _safe(exc)
            logger.warning("phone line could not prepare its identity: %s", self._last_error)

    async def aclose(self) -> None:
        for live in self._calls.values():
            self._cancel_timers(live, everything=True)
            if live.watchdog is not None:
                live.watchdog.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._save()
        if self.tel is not None:
            await self.tel.aclose()
        if self._http is not None:
            await self._http.aclose()

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:                              # a background step must report, never crash the loop
                self._last_error = _safe(exc)
                logger.warning("phone line: %s failed: %s", name, self._last_error)

        task = asyncio.create_task(guarded(), name=f"phone-{name}")
        task.add_done_callback(lambda t: coro.close() if t.cancelled() else None)     # cancelled before it ever started
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ urls and identity
    @property
    def _base(self) -> str:
        return self.settings.phone_public_base_url.strip().rstrip("/")

    def callback_url(self, call_id: str) -> str:
        return f"{self._base}/api/v1/webhooks/phone/calls/{call_id}?t={wire.callback_token(self.settings.phone_webhook_secret, call_id)}"

    def events_url(self) -> str:
        """The Event Grid endpoint (carries the secret: print it masked, never log it)."""
        return f"{self._base}/api/v1/webhooks/phone/events?key={self.settings.phone_webhook_secret}"

    def _cognitive(self) -> str | None:
        return self.settings.phone_cognitive_endpoint.strip() or None

    async def ensure_line(self) -> str:
        """The ACS identity the browser dialer calls: configured, remembered, or created on first use."""
        if self._line:
            return self._line
        if self.tel is None:
            raise TelephonyError(self._problem or "the phone line is not configured")
        async with self._line_lock:
            if not self._line:
                issued = await self.tel.issue_token(None)          # creating an identity is all that is needed here
                self._line = issued.identity
                self._save()
                logger.info("created the phone line identity %s", wire.mask(self._line))
        return self._line

    def _accepted(self) -> list[str]:
        extra = [x.strip() for x in self.settings.phone_accept_to.split(",") if x.strip()]
        return ([self._line] if self._line else []) + extra

    def _accepts(self, callee: wire.Party) -> bool:
        return any(wire.same_party(callee.raw_id, mine) for mine in self._accepted())

    # ------------------------------------------------------------------ status
    def status(self) -> PhoneStatus:
        s = self.settings
        problem = self._problem
        configured = self.tel is not None and problem is None
        active = [self._calls[i].pub for i in reversed(self._order) if i in self._calls and self._calls[i].pub.state in _LIVE_STATES]
        recent = [self._calls[i].pub for i in reversed(self._order)
                  if i in self._calls and self._calls[i].pub.state not in _LIVE_STATES][:10]
        return PhoneStatus(
            mode=s.phone_mode, configured=configured, problem=problem, number=wire.display_number(s.phone_display_number) or None,
            line_identity=self._line, public_url=self._base or None, ingress_listening=self.ingress_listening,
            seller=self._seller_status(), notice_enabled=bool(s.phone_notice.strip() and self._cognitive()),
            active=[p.model_copy(deep=True) for p in active], recent=[p.model_copy(deep=True) for p in recent],
            calls=self._counters["calls"], ingested=self._counters["ingested"], failed=self._counters["failed"],
            missed=self._counters["missed"], last_event_at=self._last_event_at, last_error=self._last_error)

    def _seller_status(self) -> PhoneSeller:
        configured = self.settings.phone_seller.strip()
        if configured.startswith("teams:"):
            cached = next((u for u in self._dir_cache.values() if u is not None), None)
            return PhoneSeller(mode="teams", name=(cached.name if cached else None) or self.settings.phone_seller_name or None,
                               online=True)
        if configured.startswith("acs:"):
            return PhoneSeller(mode="console", name=self.settings.phone_seller_name or None, online=True)
        console = self._fresh_console()
        if console is not None:
            return PhoneSeller(mode="console", name=console[1].name, online=True)
        last = max(self._consoles.values(), key=lambda c: c.seen, default=None)
        return PhoneSeller(mode="console" if last else "none", name=last.name if last else None, online=False)

    def forget(self) -> None:
        """Demo reset: drops the finished calls and the counts that describe them. A call still in progress, the line, the
        consoles and the events already handled stay, so a redelivered event can never bring a call back."""
        self._order = [i for i in self._order if i in self._calls and self._calls[i].pub.state not in _TERMINAL]
        self._calls = {i: self._calls[i] for i in self._order}
        self._counters = {"calls": len(self._order), "ingested": 0, "failed": 0, "missed": 0}
        self._last_event_at = self._last_error = None
        self._save()

    # ------------------------------------------------------------------ browser tokens and seller consoles
    async def issue_token(self, request: PhoneTokenRequest) -> PhoneToken:
        if self.tel is None:
            raise TelephonyError(self._problem or "the phone line is not configured")
        line = await self.ensure_line()
        if request.identity and (wire.same_party(request.identity, line) or not request.identity.startswith("8:acs:")):
            raise ValueError("that identity cannot be used for a browser call")
        issued = await self.tel.issue_token(request.identity)
        profile = request.profile
        if request.role == "caller":
            key = wire.party_key(issued.identity)
            if profile is not None or key not in self._profiles:       # refreshing a token must not wipe the profile
                self._profiles[key] = profile or CallerProfile()
            while len(self._profiles) > PROFILES_KEPT:
                self._profiles.pop(next(iter(self._profiles)))
        name = (request.display_name or (profile.name if profile else None) or
                ("Customer" if request.role == "caller" else self.settings.phone_seller_name or "Seller"))
        return PhoneToken(identity=issued.identity, token=issued.token, expires_on=issued.expires_on, display_name=name,
                          line_identity=line, number=wire.display_number(self.settings.phone_display_number) or None)

    def register_console(self, identity: str, name: str | None, email: str | None) -> int:
        self._consoles[identity] = _Console(name=(name or "").strip() or None, email=(email or "").strip() or None, seen=utcnow())
        return int(CONSOLE_TTL.total_seconds())

    def unregister_console(self, identity: str) -> None:
        self._consoles.pop(identity, None)

    def _fresh_console(self) -> tuple[str, _Console] | None:
        now = utcnow()
        fresh = [(i, c) for i, c in self._consoles.items() if now - c.seen <= CONSOLE_TTL]
        return max(fresh, key=lambda item: item[1].seen, default=None)

    # ------------------------------------------------------------------ the seller to ring
    async def _lookup(self, ident: str) -> DirectoryUser | None:
        if ident not in self._dir_cache:
            user: DirectoryUser | None = None
            if self._directory is not None:
                try:
                    user = await self._directory(ident)
                except Exception as exc:
                    logger.info("could not read the directory entry for the seller (%s)", _safe(exc))
            self._dir_cache[ident] = user
        return self._dir_cache[ident]

    async def pick_seller(self) -> _Seller | None:
        s = self.settings
        configured = s.phone_seller.strip()
        if configured.startswith("teams:"):
            ident = configured[6:].strip()
            user = await self._lookup(ident)
            oid = user.id if user else (ident if _GUID.match(ident) else None)
            if not oid:
                logger.warning("the Teams seller %r is not an object id and could not be resolved", ident)
                return None
            name = (user.name if user else None) or s.phone_seller_name or "Seller"
            email = (user.email if user else None) or s.phone_seller_email or None
            return _Seller("teams", Target("teams", oid, name), name, email, [f"8:orgid:{oid}", oid])
        if configured.startswith("acs:"):
            raw = configured[4:].strip()
            return _Seller("console", Target("acs", raw, s.phone_seller_name or None), s.phone_seller_name or "Seller",
                           s.phone_seller_email or None, [raw])
        console = self._fresh_console()
        if console is None:
            return None
        identity, info = console
        name = info.name or s.phone_seller_name or "Seller"
        return _Seller("console", Target("acs", identity, name), name, info.email or s.phone_seller_email or None, [identity])

    # ------------------------------------------------------------------ bookkeeping
    def _note(self, live: _Live, label: str) -> None:
        live.pub.timeline = [*live.pub.timeline, PhoneCallEvent(at=utcnow(), label=label)][-TIMELINE_MAX:]
        self._last_event_at = utcnow()
        self._save()

    def _step(self, live: _Live, state: PhoneCallState, label: str) -> None:
        if live.pub.state in _TERMINAL:
            return
        live.pub.state = state
        self._note(live, label)

    def _finish(self, live: _Live, state: PhoneCallState, label: str, *, error: str | None = None) -> None:
        if live.pub.state in _TERMINAL:
            return
        self._cancel_timers(live, everything=True)
        live.pub.state = state
        if error:
            live.pub.error = error
            self._last_error = error
        if state in self._counters:
            self._counters[state] += 1
        self._note(live, label)

    def _cancel_timers(self, live: _Live, *, everything: bool = False) -> None:
        """Cancels the step timers (and with ``everything`` the call-length timer), never the task that is running this."""
        current = asyncio.current_task()
        keep = []
        for task in live.timers:
            if task is current:
                keep.append(task)
            else:
                task.cancel()
        live.timers = keep
        if everything and live.max_timer is not None and live.max_timer is not current:
            live.max_timer.cancel()
            live.max_timer = None

    def _later(self, live: _Live, delay: float, step: Callable[[_Live], Awaitable[None]], name: str, *,
               limit: bool = False) -> None:
        async def run() -> None:
            await self._sleep(delay)
            async with live.lock:
                await step(live)

        task = self._spawn(run(), name)
        if limit:
            live.max_timer = task
        else:
            live.timers.append(task)

    def _find(self, *, server_call_id: str | None = None, connection: str | None = None) -> _Live | None:
        for live in self._calls.values():
            if server_call_id and live.server_call_id == server_call_id:
                return live
            if connection and live.conn == connection:
                return live
        return None

    def _match_recording(self, ready: wire.RecordingReady) -> _Live | None:
        """The call a recording belongs to.

        The call id in the event's subject is the call's correlation id, which is what the line keys its calls by. (The
        ``serverCallId`` there is a different, shorter identifier than the one in the IncomingCall event, so it is only a
        second chance.) As a last resort, exactly one call waiting for its recording can only be this one.
        """
        live = self._calls.get(ready.call_id) if ready.call_id else None
        if live is None and ready.server_call_id:
            live = self._find(server_call_id=ready.server_call_id)
        if live is None:
            waiting = [c for c in self._calls.values() if c.pub.state == "ended" and c.pub.recording == "active"]
            live = waiting[0] if len(waiting) == 1 else None
        if live is None:
            logger.info("recording %s matches no remembered call (call %s, server call %s, %d waiting)", ready.key[:16],
                        (ready.call_id or "-")[:8], (ready.server_call_id or "-")[:8],
                        sum(1 for c in self._calls.values() if c.pub.state == "ended"))
        return live

    # ------------------------------------------------------------------ Event Grid
    async def handle_events(self, events: list[dict[str, Any]]) -> None:
        """Event Grid notifications. Returns quickly: the work continues in the background (the call is ringing)."""
        for event in events:
            kind = wire.event_type(event)
            if kind == wire.EVENT_INCOMING_CALL:
                incoming = wire.parse_incoming_call(event)
                if incoming is not None:
                    self._on_incoming(incoming)
            elif kind == wire.EVENT_RECORDING_READY:
                ready = wire.parse_recording_ready(event)
                if ready is not None:
                    self._last_event_at = utcnow()
                    self._spawn(self._on_recording(ready), "recording")

    def _on_incoming(self, inc: wire.IncomingCall) -> None:
        if not self._accepts(inc.callee):
            logger.debug("ignoring a call to %s (not the VoiceCRM line)", wire.mask(inc.callee.raw_id))
            return
        if inc.correlation_id in self._calls or inc.correlation_id in self._seen_calls:
            return                                                  # Event Grid delivers at least once
        self._seen_calls = [*self._seen_calls, inc.correlation_id][-SEEN_KEPT:]
        kind: Literal["phone", "voip"] = "phone" if inc.caller.kind == "phone" else "voip"
        profile = self._profiles.get(wire.party_key(inc.caller.raw_id)) if kind == "voip" else None
        name = (profile.name if profile and profile.name else None) or inc.caller_name
        shown = (inc.caller.phone or inc.caller.raw_id) if kind == "phone" else (name or "Browser caller")
        pub = PhoneCall(call_id=inc.correlation_id, state="ringing", caller=shown, caller_kind=kind,
                        caller_name=name if kind == "voip" else None, started_at=utcnow())
        live = _Live(pub=pub, caller=inc.caller, context=inc.incoming_call_context, profile=profile,
                     server_call_id=inc.server_call_id)
        self._calls[pub.call_id] = live
        self._order.append(pub.call_id)
        self._counters["calls"] += 1
        self._last_error = None                                    # the status reports the latest problem, not an old one
        self._trim()
        self._note(live, f"Call received from {wire.mask(shown) if kind == 'phone' else shown}")
        self._spawn(self._answer(live), "answer")

    def _trim(self) -> None:
        """Forgets the oldest finished calls so a long-running server does not collect them forever."""
        while len(self._order) > RECENT_KEPT * 4:
            oldest = self._order.pop(0)
            live = self._calls.get(oldest)
            if live is not None and live.pub.state in _LIVE_STATES:
                self._order.append(oldest)
                break
            self._calls.pop(oldest, None)

    async def _answer(self, live: _Live) -> None:
        async with live.lock:
            if self.tel is None:
                return
            try:
                answered = await self.tel.answer(live.context, self.callback_url(live.pub.call_id),
                                                 cognitive_endpoint=self._cognitive())
            except TelephonyError as exc:
                self._finish(live, "missed", "Could not answer the call", error=str(exc))
                return
            finally:
                live.context = ""
            live.conn = answered.call_connection_id
            live.server_call_id = answered.server_call_id or live.server_call_id
            self._step(live, "connecting", "Answered the call")

    # ------------------------------------------------------------------ Call Automation callbacks
    def dispatch_callbacks(self, call_id: str, callbacks: list[wire.Callback]) -> None:
        """Handles callbacks in the background (in arrival order per call) so the webhook can answer ACS at once."""
        self._spawn(self.handle_callbacks(call_id, callbacks), "callback")

    async def handle_callbacks(self, call_id: str, callbacks: list[wire.Callback]) -> None:
        live = self._calls.get(call_id)
        if live is None:
            logger.debug("callback for an unknown call %s", call_id[:8])
            return
        for callback in callbacks:
            async with live.lock:
                try:
                    await self._on_callback(live, callback)
                except Exception as exc:
                    logger.warning("phone call %s: handling %s failed: %s", call_id[:8], callback.kind, _safe(exc))
                    self._finish(live, "failed", f"Error while handling {callback.kind}", error=_safe(exc))
                    await self._hang_up(live)

    async def _on_callback(self, live: _Live, cb: wire.Callback) -> None:
        live.conn = cb.call_connection_id or live.conn
        live.server_call_id = cb.server_call_id or live.server_call_id
        kind, context = cb.kind, cb.operation_context
        if kind == "CallConnected":
            if live.pub.connected_at is None:
                live.pub.connected_at = utcnow()
                self._note(live, "Call connected")
                await self._play_notice(live)
        elif kind == "PlayCompleted" and context == "notice":
            await self._notice_done(live, played=True)
        elif kind in ("PlayFailed", "PlayCanceled") and context == "notice":
            await self._notice_done(live, played=False, why=cb.result_message or "the prompt could not be played")
        elif kind == "RecordingStateChanged":
            if cb.recording_state == "active" and live.pub.recording != "active":
                live.pub.recording = "active"
                self._note(live, "Recording started")
        elif kind == "AddParticipantSucceeded":
            live.seller_joined = True
            self._cancel_timers(live)
            who = live.pub.seller or "The seller"
            self._step(live, "in_call", f"{who} joined the call")
        elif kind == "AddParticipantFailed":
            await self._voicemail(live, cb.result_message or "the seller could not be reached")
        elif kind == "ParticipantsUpdated":
            await self._on_participants(live, cb)
        elif kind == "CallDisconnected":
            await self._on_disconnected(live)

    async def _play_notice(self, live: _Live) -> None:
        text = self.settings.phone_notice.strip()
        if not (text and self._cognitive() and self.tel is not None and live.conn):
            await self._notice_done(live, played=False, why="no voice connection is configured", skipped=True)
            return
        self._step(live, "notice", "Playing the recording notice")
        try:
            await self.tel.play_text(live.conn, text, voice=self.settings.phone_notice_voice, locale="en-US", context="notice")
        except TelephonyError as exc:
            await self._notice_done(live, played=False, why=str(exc))
            return
        self._later(live, NOTICE_WAIT_SECONDS, self._notice_timeout, "notice-timeout")

    async def _notice_timeout(self, live: _Live) -> None:
        if live.pub.notice == "pending":
            await self._notice_done(live, played=False, why="the prompt did not finish in time")

    async def _notice_done(self, live: _Live, *, played: bool, why: str = "", skipped: bool = False) -> None:
        if live.pub.notice != "pending" or live.pub.state in _TERMINAL:
            return
        self._cancel_timers(live)
        if played:
            live.pub.notice = "played"
            self._note(live, "Recording notice played")
        else:
            live.pub.notice = "skipped" if skipped else "failed"
            if self.settings.phone_require_notice:
                await self._hang_up(live)
                self._finish(live, "failed", f"Recording notice unavailable: {why}", error=f"the recording notice could not be played ({why})")
                return
            self._note(live, f"Recording without the notice ({why})")
        await self._start_recording_and_ring(live)

    async def _start_recording_and_ring(self, live: _Live) -> None:
        if self.tel is None or not live.conn:
            return
        seller = await self.pick_seller()
        live.seller = seller
        live.pub.seller = seller.name if seller else None
        caller_target = (Target("phone", live.caller.phone or live.caller.raw_id) if live.caller.kind == "phone"
                         else Target("acs", live.caller.raw_id))
        channels = [caller_target] + ([seller.target] if seller else [])
        try:
            live.recording_id = await self.tel.start_recording(live.conn, state_callback_url=self.callback_url(live.pub.call_id),
                                                               channels=channels)
        except TelephonyError as exc:
            live.pub.recording = "failed"
            await self._hang_up(live)
            self._finish(live, "failed", "Could not start the recording", error=str(exc))
            return
        live.pub.recording = "active"
        self._note(live, "Recording started (one channel per participant)")
        self._later(live, self.settings.phone_max_call_minutes * 60, self._max_duration, "max-duration", limit=True)
        if seller is None:
            await self._voicemail(live, "no seller is available")
            return
        await self._ring(live, seller)

    async def _ring(self, live: _Live, seller: _Seller) -> None:
        assert self.tel is not None and live.conn
        label = "Teams" if seller.kind == "teams" else "browser console"
        self._step(live, "calling_seller", f"Ringing {seller.name or 'the seller'} ({label})")
        try:
            live.invitation = await self.tel.add_participant(
                live.conn, seller.target, timeout_seconds=self.settings.phone_ring_seconds,
                display_name=live.pub.caller_name or live.pub.caller, context="seller")
        except TelephonyError as exc:
            await self._voicemail(live, str(exc))
            return
        self._later(live, self.settings.phone_ring_seconds + 5, self._ring_timeout, "ring-timeout")

    async def _ring_timeout(self, live: _Live) -> None:
        if live.pub.state == "calling_seller":
            if self.tel is not None and live.conn and live.invitation:
                try:
                    await self.tel.cancel_invite(live.conn, live.invitation)
                except TelephonyError:
                    logger.debug("could not cancel the invitation", exc_info=True)
            await self._voicemail(live, "no one answered")

    async def _voicemail(self, live: _Live, why: str) -> None:
        if live.pub.state in _TERMINAL or live.pub.state in ("leaving_message", "in_call", "ended"):
            return
        self._cancel_timers(live)
        self._step(live, "leaving_message", f"Offering voicemail ({why})")
        if self.tel is not None and live.conn and self._cognitive():
            try:
                await self.tel.play_text(live.conn, VOICEMAIL_PROMPT, voice=self.settings.phone_notice_voice,
                                         locale="en-US", context="voicemail")
            except TelephonyError:
                logger.debug("could not play the voicemail prompt", exc_info=True)
        self._later(live, self.settings.phone_voicemail_seconds, self._message_limit, "message-limit")

    async def _message_limit(self, live: _Live) -> None:
        if live.pub.state == "leaving_message":
            self._note(live, "Message length limit reached")
            await self._hang_up(live)

    async def _max_duration(self, live: _Live) -> None:
        if live.pub.state in _LIVE_STATES:
            self._note(live, "Maximum call length reached")
            await self._hang_up(live)

    async def _on_participants(self, live: _Live, cb: wire.Callback) -> None:
        present = [p.raw_id for p in cb.participants]
        seller_ids = live.seller.ids if live.seller else []
        caller_here = any(wire.same_party(p, live.caller.raw_id) for p in present)
        seller_here = any(wire.same_party(p, sid) for p in present for sid in seller_ids)
        live.caller_seen = live.caller_seen or caller_here
        live.seller_joined = live.seller_joined or seller_here
        if live.pub.state in _TERMINAL or live.pub.state == "ended":
            return
        if live.caller_seen and not caller_here:
            self._note(live, "The caller hung up")
            await self._hang_up(live)
        elif live.seller_joined and not seller_here and live.pub.state == "in_call":
            self._note(live, "The seller hung up")
            await self._hang_up(live)

    async def _hang_up(self, live: _Live) -> None:
        if live.hang_up_sent or self.tel is None or not live.conn:
            return
        live.hang_up_sent = True
        try:
            await self.tel.hang_up(live.conn)
        except TelephonyError:
            logger.debug("hang up failed (the call may already be over)", exc_info=True)

    async def _on_disconnected(self, live: _Live) -> None:
        if live.pub.ended_at is not None:
            return
        live.pub.ended_at = utcnow()
        if live.pub.connected_at:
            live.pub.duration_seconds = round((live.pub.ended_at - live.pub.connected_at).total_seconds(), 1)
        self._cancel_timers(live, everything=True)
        if live.pub.state in _TERMINAL:
            self._save()
            return
        if live.recording_id or live.pub.recording == "active":
            took = f" ({live.pub.duration_seconds:.0f} s)" if live.pub.duration_seconds else ""
            self._step(live, "ended", f"Call ended{took}; waiting for the recording")
            live.watchdog = self._spawn(self._watch_recording(live), "recording-watchdog")
        else:
            self._finish(live, "missed", "The caller hung up before the recording started")

    async def _watch_recording(self, live: _Live) -> None:
        await self._sleep(RECORDING_WAIT_SECONDS)
        if live.pub.state == "ended":
            live.pub.recording = "failed"
            self._finish(live, "failed", "No recording arrived", error="no recording arrived within 15 minutes of the call ending")

    # ------------------------------------------------------------------ the recording
    async def _on_recording(self, ready: wire.RecordingReady) -> None:
        if ready.key in self._seen_recordings:
            return
        self._seen_recordings = [*self._seen_recordings, ready.key][-SEEN_KEPT:]
        live = self._match_recording(ready)
        if live is None:                                           # e.g. the app restarted: describe the call from the recording
            started = ready.started_at or utcnow()
            pub = PhoneCall(call_id=ready.call_id or ready.key, state="ended", caller="Unknown caller", caller_kind="voip",
                            started_at=started, ended_at=started + timedelta(milliseconds=ready.duration_ms or 0),
                            recording="ready")
            live = _Live(pub=pub, caller=wire.Party(), server_call_id=ready.server_call_id)
            self._calls[pub.call_id] = live
            self._order.append(pub.call_id)
            self._counters["calls"] += 1
        if live.watchdog is not None:
            live.watchdog.cancel()
        self._reopen(live)
        live.pub.recording = "ready"
        self._step(live, "processing", f"Recording ready ({len(ready.chunks)} file{'s' if len(ready.chunks) > 1 else ''}); downloading")
        try:
            audio, plan, speech = await self._fetch_audio(ready, live)
            floor = self.settings.phone_min_speech_seconds
            if speech < floor:                                     # a hang-up or a silent voicemail: nothing to transcribe or pay for
                self._finish(live, "missed", f"No speech in the recording ({speech:.1f} s of speech-level audio, {floor:g} s "
                                             "needed): nothing was sent to the pipeline")
                await self._delete_recording(ready)
                return
            ingest = self._build_ingest(ready, live, audio, plan)
            assert self.service is not None
            result, running = await self.service.start(ingest)
        except Exception as exc:
            live.pub.recording = "failed"
            logger.warning("phone recording %s could not be ingested: %s", ready.key[:16], _safe(exc))
            self._finish(live, "failed", "The recording could not be processed", error=_safe(exc))
            self._seen_recordings = [k for k in self._seen_recordings if k != ready.key]       # a re-delivery may try again
            return
        live.pub.conversation_id = result.duplicate_of or result.conversation_id
        how = "already in the Inbox" if result.duplicate_of else f"audio channels: {plan.note}"
        await self._delete_recording(ready)                        # the pipeline has the audio now
        if running is not None:                                    # report what the pipeline made of it, not only that it took it
            self._note(live, f"Handed to the pipeline ({how}); waiting for the result")
            final = await asyncio.shield(running)
            if final.status == ConversationStatus.failed and final.error == NO_SPEECH_MESSAGE:
                self._finish(live, "missed", "The speech service found no speech in the recording")
                return
            if final.status == ConversationStatus.failed:
                self._finish(live, "failed", "The pipeline could not process the recording", error=final.error)
                return
        self._finish(live, "ingested", f"Sent through the pipeline ({how})")

    def _reopen(self, live: _Live) -> None:
        """A recording that turns up after its call was given up on (a late event, a retry) is still processed."""
        if live.pub.state in ("failed", "missed"):
            self._counters[live.pub.state] = max(0, self._counters[live.pub.state] - 1)
            live.pub.state, live.pub.error = "ended", None

    async def _fetch_audio(self, ready: wire.RecordingReady, live: _Live) -> tuple[bytes, wire.ChannelPlan, float]:
        """The audio to transcribe, how its channels were planned, and the seconds of speech-level audio in it."""
        assert self.tel is not None
        seller_ids = live.seller.ids if live.seller else []
        rendered: list[tuple[wire.ChannelPlan, bytes]] = []
        speech = 0.0
        for chunk in ready.chunks:
            if not wire.is_recording_url(chunk.content_location):
                raise TelephonyError("the recording event points outside the recording service; ignored")
            content = await self.tel.download(chunk.content_location)
            meta = wire.RecordingMetadata()
            if chunk.metadata_location:
                try:
                    meta = wire.parse_recording_metadata(await self.tel.download(chunk.metadata_location))
                except (TelephonyError, ValueError) as exc:
                    logger.info("recording metadata unavailable (%s); channels are matched by activity", _safe(exc))
            plan, audio, heard = await asyncio.to_thread(self._render, content, meta, live.caller.raw_id or None, seller_ids,
                                                         self._accepted(), self.settings.phone_min_speech_seconds)
            logger.info("recording %s chunk %s: participants on channels %s -> %s (%s); speech-level audio %.1f s (counted up to %.1f s)",
                        ready.key[:16], chunk.index, [(wire.mask(p.participant_id), p.channel) for p in meta.participants],
                        plan.mode, plan.note, heard, self.settings.phone_min_speech_seconds)
            rendered.append((plan, audio))
            speech += heard
        if len(rendered) == 1:
            plan, audio = rendered[0]
            return audio, plan, speech
        if len({plan.mode for plan, _ in rendered}) == 1:
            return wire.concat_wavs([audio for _, audio in rendered]), rendered[0][0], speech
        logger.warning("the recording's chunks use different channel layouts; only the first is processed")
        return rendered[0][1], rendered[0][0], speech

    @staticmethod
    def _render(content: bytes, meta: wire.RecordingMetadata, caller_id: str | None, seller_ids: list[str],
                line_ids: list[str], needed: float) -> tuple[wire.ChannelPlan, bytes, float]:
        """Plans the channels, renders the audio and measures its speech (counted only up to ``needed`` seconds)."""
        pcm = wire.read_pcm16(content)
        plan = wire.plan_channels(meta, wire.channel_levels(pcm), caller_id=caller_id, seller_ids=seller_ids, line_ids=line_ids)
        heard = wire.speech_seconds(pcm, wire.plan_sources(pcm, plan), stop_at=needed) if needed > 0 else 0.0
        return plan, wire.render_plan(pcm, plan), heard

    def _build_ingest(self, ready: wire.RecordingReady, live: _Live, audio: bytes, plan: wire.ChannelPlan) -> IngestInput:
        s = self.settings
        profile = live.profile or CallerProfile()
        caller_phone = live.caller.phone if live.caller.kind == "phone" else None
        number = caller_phone or profile.phone
        customer_name = profile.name or (wire.display_number(caller_phone) if caller_phone else None) or live.pub.caller_name or "Caller"
        customer = Participant(id=CUSTOMER_ID, display_name=customer_name, email=profile.email, phone=number,
                               role=SpeakerRole.customer, org=profile.org)
        seller_info = live.seller
        seller = Participant(id=SELLER_ID, display_name=(seller_info.name if seller_info else None) or s.phone_seller_name or "Seller",
                             email=(seller_info.email if seller_info else None) or s.phone_seller_email or None,
                             role=SpeakerRole.seller)
        started = live.pub.connected_at or ready.started_at or live.pub.started_at         # the call, not the first sound in it
        duration = (live.pub.duration_seconds if live.pub.duration_seconds is not None
                    else (ready.duration_ms / 1000.0) if ready.duration_ms else None)
        heard = live.pub.notice == "played"
        meta = ConversationMeta(
            source=Channel.phone, source_interaction_id=ready.key, direction=Direction.inbound,
            title=f"Phone call from {customer_name}", started_at=started,
            ended_at=live.pub.ended_at or (started + timedelta(seconds=duration) if duration else None), duration_seconds=duration,
            seller=seller, participants=[customer], customer_org=profile.org, market=s.default_market,
            business_line=s.default_business_line,
            consent=ConsentInfo(status="recorded_with_consent" if heard else "unknown",
                                basis="Recording notice played to the caller before recording started" if heard
                                else "The recording notice was not confirmed as played"))
        return IngestInput(
            meta=meta, kind="audio", audio_bytes=audio, audio_filename=f"phone-{ready.key[-12:]}.wav", audio_content_type="audio/wav",
            channel_speakers=[CUSTOMER_ID, SELLER_ID] if plan.mode in ("stereo", "caller_only") else [])

    async def _delete_recording(self, ready: wire.RecordingReady) -> None:
        """The audio now lives in the pipeline's vault (or is discarded by policy): drop the service's temporary copy."""
        if self.tel is None:
            return
        for chunk in ready.chunks:
            if chunk.delete_location and wire.is_recording_url(chunk.delete_location):
                try:
                    await self.tel.delete_recording(chunk.delete_location)
                except TelephonyError:
                    logger.debug("could not delete the recording's temporary copy", exc_info=True)

    # ------------------------------------------------------------------ self-check (the preflight and `phone check` use it)
    async def checks(self) -> list[tuple[str, Literal["pass", "warn", "fail", "skip"], str, str]]:
        """(name, status, detail, hint) for each thing the line depends on."""
        s = self.settings
        out: list[tuple[str, Literal["pass", "warn", "fail", "skip"], str, str]] = []
        try:
            line = await self.ensure_line()
            out.append(("phone: ACS access", "pass", f"signed in to the Communication Services resource; line identity {wire.mask(line)}", ""))
        except Exception as exc:
            out.append(("phone: ACS access", "fail", _safe(exc)[:280],
                        "check VOICECRM_PHONE_ACS_ENDPOINT, `az login`, and the Communication Services data-plane access"))
        out.append(await self._check_public_url())
        if s.phone_seller.strip().startswith("teams:"):
            seller = await self.pick_seller()
            out.append(("phone: seller", "pass" if seller else "fail",
                        f"rings {seller.name} in Teams" if seller else "the Teams seller could not be resolved",
                        "" if seller else "set VOICECRM_PHONE_SELLER=teams:<the user's object id>"))
        elif self._fresh_console() is not None:
            _, console = self._fresh_console()  # type: ignore[misc]
            out.append(("phone: seller", "pass", f"{console.name or 'a seller'} is online in a browser console", ""))
        else:
            out.append(("phone: seller", "warn", "no seller is available: callers are offered voicemail",
                        "turn on 'Answer calls in this browser' in Live demo, section D, or set VOICECRM_PHONE_SELLER"))
        if s.phone_notice.strip() and self._cognitive():
            out.append(("phone: recording notice", "pass", f"spoken with {s.phone_notice_voice} through the Foundry connection", ""))
        else:
            level: Literal["warn", "fail"] = "fail" if s.phone_require_notice else "warn"
            out.append(("phone: recording notice", level, "the notice cannot be spoken: no VOICECRM_PHONE_COGNITIVE_ENDPOINT",
                        "connect the Communication Services identity to the Foundry resource (docs/phone-line.md), or set "
                        "VOICECRM_PHONE_REQUIRE_NOTICE=false"))
        return out

    async def _check_public_url(self) -> tuple[str, Literal["pass", "warn", "fail", "skip"], str, str]:
        name = "phone: public URL"
        if not self._base:
            return name, "fail", "VOICECRM_PHONE_PUBLIC_BASE_URL is not set", "start the dev tunnel and set the URL (docs/phone-line.md)"
        client = self._http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=6.0))
        try:
            response = await client.get(f"{self._base}/api/v1/webhooks/phone/health")
            if response.status_code == 200 and response.json().get("service") == "voicecrm-phone":
                return name, "pass", f"{self._base.split('//', 1)[-1].split('/')[0]} reaches the phone ingress", ""
            return (name, "fail", f"{self._base} answered HTTP {response.status_code}, not the phone ingress",
                    "the tunnel must forward to the ingress port (VOICECRM_PHONE_INGRESS_PORT), not the API port")
        except (httpx.HTTPError, ValueError) as exc:
            return (name, "fail", f"{type(exc).__name__}: cannot reach {self._base}",
                    "start the tunnel (scripts\\run_phone_tunnel.bat) and make sure the backend is running")
        finally:
            if self._http is None:
                await client.aclose()

"""Live Microsoft Teams ingestion: finds new meeting transcripts through Microsoft Graph and feeds them to the pipeline.

Two ways to learn that a transcript exists, one way to ingest it:

  poll      every few seconds ask `getAllTranscripts` for each configured organiser (works behind any firewall; no public URL);
  webhook   Graph POSTs a change notification to /api/v1/webhooks/teams once a transcript is available (needs a public URL).

Either way `ingest_transcript` downloads the speaker-attributed WebVTT, reads the meeting (subject, attendees), maps it to the
same `IngestInput` an uploaded Teams transcript produces, and submits it to the pipeline, which de-duplicates on the Graph
transcript id, so re-delivery, a restart or both paths running together never create a second conversation.

Graph makes a transcript available some minutes after the meeting ends, later than the Teams chat shows it (one Microsoft Q&A
report says about 20 minutes; there is no guaranteed time), and only for calendar-backed meetings, not "Meet now" ones. It is
not a live stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..adapters.sources import detect_and_parse
from ..adapters.sources.teams import TranscriptRef
from ..adapters.sources.teams_graph import ClientSecretAuth, GraphClient, GraphError
from ..config import Settings
from ..domain.base import VModel
from ..domain.conversation import ConsentInfo, ConversationMeta, Participant, utcnow
from ..domain.enums import Channel, Direction, SpeakerRole, TranscriptOrigin
from .models import IngestInput
from .service import PipelineService

logger = logging.getLogger(__name__)

_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
UNFINISHED_GRACE = timedelta(minutes=10)         # a transcript without an end time is only trusted once it is this old
RETRY_AFTER_FAILURE = timedelta(seconds=60)
SEEN_RETENTION = timedelta(days=7)
MAX_RECENT = 8


class TeamsRecent(VModel):
    transcript_id: str
    title: str | None = None
    conversation_id: str | None = None
    at: datetime
    outcome: str                                    # ingested | duplicate | failed
    detail: str | None = None


class TeamsStatus(VModel):
    mode: str = "off"                               # off | poll | webhook
    configured: bool = False
    problem: str | None = None                      # why it cannot run (missing settings)
    organizers: list[str] = []
    poll_seconds: int = 0
    notification_url: str | None = None             # where Graph should POST in webhook mode
    last_poll_at: datetime | None = None
    last_poll_ok: bool | None = None
    last_error: str | None = None
    last_ingest_at: datetime | None = None
    ingested: int = 0
    duplicates: int = 0
    failed: int = 0
    recent: list[TeamsRecent] = []


@dataclass(frozen=True)
class _User:
    id: str
    name: str | None = None
    mail: str | None = None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _name_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return " ".join(part.capitalize() for part in re.split(r"[._-]+", email.split("@")[0]) if part) or None


def _ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _name_from_alias(email: str | None, labels: list[str]) -> str | None:
    """The one transcript speaker label that an e-mail alias abbreviates ("AishaW" -> "Aisha West"), else None.

    Graph's application permissions return no display names (that needs User.Read.All), but a Teams transcript labels
    every speaker with theirs, so a corporate alias (first name + last initial, "aisha.west", "awest") can be matched
    against those labels. Two speakers matching the same alias is ambiguous, so it names nobody.
    """
    if not email or "@" not in email:
        return None
    alias = re.sub(r"[^a-z]", "", _ascii_lower(email.split("@")[0]))
    if not alias:
        return None
    found: list[str] = []
    for label in dict.fromkeys(labels):
        words = re.findall(r"[a-z]+", _ascii_lower(label))
        if len(words) < 2:
            continue
        first, last = words[0], words[-1]
        if alias in {first + last[:n] for n in range(1, len(last) + 1)} | {first[0] + last}:
            found.append(label)
    return found[0] if len(found) == 1 else None


class TeamsIngestor:
    def __init__(self, service: PipelineService | None, graph: GraphClient | None, *, settings: Settings,
                 state_file: Path | None = None, problem: str | None = None) -> None:
        """``service=None`` gives a read-only ingestor (the preflight probe uses it to resolve organisers)."""
        self.service, self.graph, self.settings = service, graph, settings
        self._organizers = [o.strip() for o in settings.teams_organizers.split(",") if o.strip()]
        self._poll_seconds = max(5, settings.teams_poll_seconds)
        self._lookback = timedelta(minutes=max(10, settings.teams_lookback_minutes))
        self._state_file = state_file
        self._users: dict[str, _User] = {}
        self._seen = self._load_seen()
        self._retry_at: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        base = settings.teams_public_base_url.rstrip("/")
        self._status = TeamsStatus(
            mode=settings.teams_mode, configured=graph is not None and problem is None, problem=problem,
            organizers=list(self._organizers), poll_seconds=self._poll_seconds,
            notification_url=f"{base}/api/v1/webhooks/teams" if base else None)

    # ------------------------------------------------------------------ construction / lifecycle
    @classmethod
    def from_settings(cls, service: PipelineService | None, settings: Settings, *,
                      http_client: httpx.AsyncClient | None = None) -> TeamsIngestor:
        state_file = settings.state_dir / "teams_seen.json"
        try:
            auth = ClientSecretAuth(tenant_id=settings.teams_tenant_id, client_id=settings.teams_client_id,
                                    client_secret=settings.teams_client_secret, http_client=http_client)
        except ValueError as exc:
            return cls(service, None, settings=settings, state_file=state_file, problem=str(exc))
        return cls(service, GraphClient(auth, http_client=http_client), settings=settings, state_file=state_file)

    def start(self) -> None:
        if self.settings.teams_mode == "poll" and self.graph is not None and self._task is None:
            self._task = asyncio.create_task(self._run(), name="teams-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.graph is not None:
            await self.graph.aclose()

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning("Teams poll failed: %s", exc)
            await asyncio.sleep(min(self._poll_seconds * 2 ** min(failures, 4), 300) if failures else self._poll_seconds)

    def status(self) -> TeamsStatus:
        return self._status.model_copy(deep=True)

    # ------------------------------------------------------------------ persistence of what was already ingested
    def _load_seen(self) -> dict[str, datetime]:
        if self._state_file is None or not self._state_file.exists():
            return {}
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            cutoff = utcnow() - SEEN_RETENTION
            seen = {k: t for k, v in raw.items() if (t := _parse_time(v)) and t > cutoff}
            return seen
        except (OSError, ValueError, AttributeError):
            logger.warning("could not read %s; transcripts may be re-checked (the pipeline still de-duplicates)", self._state_file)
            return {}

    def _remember(self, key: str) -> None:
        self._seen[key] = utcnow()
        if self._state_file is None:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({k: _iso(t) for k, t in self._seen.items()}), encoding="utf-8")
        except OSError:
            logger.debug("could not write %s", self._state_file, exc_info=True)

    # ------------------------------------------------------------------ discovery
    def _require_graph(self) -> GraphClient:
        if self.graph is None:
            raise GraphError(self._status.problem or "Teams live ingestion is not configured")
        return self.graph

    async def _user(self, ident: str) -> _User:
        if ident in self._users:
            return self._users[ident]
        graph = self._require_graph()
        try:
            body = await graph.get_json(f"/users/{quote(ident, safe='@.')}",
                                        params={"$select": "id,displayName,mail,userPrincipalName"})
            user = _User(str(body["id"]), body.get("displayName"), body.get("mail") or body.get("userPrincipalName"))
        except GraphError as exc:
            if not _GUID.match(ident):
                raise GraphError(f"could not resolve organiser {ident}: {exc.message}", status=exc.status,
                                 code=exc.code, hint="check the address, or put the user's object id in "
                                 "VOICECRM_TEAMS_ORGANIZERS (resolving a UPN needs User.Read.All)") from exc
            user = _User(ident)                      # an object id is enough; the name and address are a nicety
        self._users[ident] = user
        return user

    async def poll_once(self) -> int:
        """Checks every organiser for new transcripts and ingests them. Returns how many conversations were created."""
        graph = self._require_graph()
        if not self._organizers:
            raise GraphError("no organisers configured", hint="set VOICECRM_TEAMS_ORGANIZERS to the UPN or object id of "
                             "each seller who hosts the meetings")
        async with self._lock:
            now = utcnow()
            since = _iso(now - self._lookback)
            created = 0
            self._status.last_error = None
            try:
                for ident in self._organizers:
                    user = await self._user(ident)
                    path = (f"/users/{user.id}/onlineMeetings/getAllTranscripts"
                            f"(meetingOrganizerUserId='{user.id}',startDateTime={since})")
                    async for item in graph.paged(path, params={"$top": 50}):
                        try:
                            created += await self._consider(user, item)
                        except GraphError as exc:
                            self._status.last_error = str(exc)[:500]
            except Exception as exc:
                self._status.last_error = str(exc)[:500]
                raise
            finally:
                self._status.last_poll_at = now
                self._status.last_poll_ok = self._status.last_error is None
            return created

    @staticmethod
    def _finished(item: dict[str, Any]) -> bool:
        if _parse_time(item.get("endDateTime")):
            return True
        started = _parse_time(item.get("createdDateTime"))
        return bool(started and utcnow() - started >= UNFINISHED_GRACE)

    async def _consider(self, user: _User, item: dict[str, Any]) -> int:
        meeting_id, transcript_id = item.get("meetingId"), item.get("id")
        if not meeting_id or not transcript_id:
            return 0
        key = f"{meeting_id}|{transcript_id}"
        if key in self._seen or not self._finished(item) or self._retry_at.get(key, utcnow()) > utcnow():
            return 0
        return await self.ingest_transcript(user, str(meeting_id), str(transcript_id))

    async def ingest_refs(self, refs: list[TranscriptRef]) -> int:
        """Webhook path: ingest what a Graph change notification points at. Ad hoc call transcripts are not supported."""
        created = 0
        for ref in refs:
            if ref.kind != "online_meeting":
                logger.info("ignoring an ad hoc call transcript notification (only scheduled meetings are ingested)")
                continue
            candidates = [ref.organizer_id] if ref.organizer_id else list(self._organizers)
            if not candidates:
                self._status.last_error = ("a notification arrived without an organiser and VOICECRM_TEAMS_ORGANIZERS is empty, "
                                           "so the transcript could not be located")
                continue
            last_error: GraphError | None = None
            for ident in candidates:
                try:
                    created += await self.ingest_transcript(await self._user(ident), ref.meeting_id, ref.transcript_id)
                    last_error = None
                    break
                except GraphError as exc:
                    last_error = exc
                    if exc.status not in (403, 404):
                        break
            if last_error is not None:
                self._status.last_error = str(last_error)[:500]
        return created

    # ------------------------------------------------------------------ one transcript
    async def ingest_transcript(self, user: _User, meeting_id: str, transcript_id: str) -> int:
        """Downloads, maps and submits one transcript. Returns 1 when a new conversation was created, else 0."""
        graph = self._require_graph()
        key = f"{meeting_id}|{transcript_id}"
        base = f"/users/{quote(user.id, safe='')}/onlineMeetings/{quote(meeting_id, safe='')}"
        try:
            vtt = await graph.get_text(f"{base}/transcripts/{quote(transcript_id, safe='')}/content", accept="text/vtt")
            meeting = await self._meeting(base)
            ingest = self._ingest_input(user, meeting, transcript_id, vtt)
            result = await self.service.submit(ingest)
        except (GraphError, ValueError) as exc:
            self._retry_at[key] = utcnow() + RETRY_AFTER_FAILURE
            self._status.failed += 1
            self._note(transcript_id, None, "failed", str(exc)[:240], None)
            if isinstance(exc, ValueError):
                self._status.last_error = f"the transcript could not be read: {exc}"
                logger.warning("Teams transcript %s could not be parsed: %s", transcript_id[:12], exc)
                return 0
            raise
        self._remember(key)
        title = ingest.meta.title
        if result.duplicate_of:
            self._status.duplicates += 1
            self._note(transcript_id, result.duplicate_of, "duplicate", None, title)
            return 0
        self._status.ingested += 1
        self._status.last_ingest_at = utcnow()
        self._note(transcript_id, result.conversation_id, "ingested", None, title)
        logger.info("Teams transcript %s ingested as %s", transcript_id[:12], result.conversation_id)
        return 1

    async def _meeting(self, base: str) -> dict[str, Any]:
        """Subject, times and attendees; best effort, because the transcript alone is enough to proceed."""
        try:
            return await self._require_graph().get_json(base)
        except GraphError as exc:
            logger.info("Teams meeting details unavailable (%s); continuing with the transcript only", exc.status)
            return {}

    def _note(self, transcript_id: str, conversation_id: str | None, outcome: str, detail: str | None,
              title: str | None) -> None:
        self._status.recent = [TeamsRecent(transcript_id=transcript_id[:12], title=title, conversation_id=conversation_id,
                                           at=utcnow(), outcome=outcome, detail=detail),
                               *self._status.recent][:MAX_RECENT]

    def _ingest_input(self, user: _User, meeting: dict[str, Any], transcript_id: str, vtt: str) -> IngestInput:
        parsed = detect_and_parse(f"teams-{transcript_id[:16]}.vtt", "text/vtt", vtt.encode("utf-8"), channel=Channel.teams)
        participants = meeting.get("participants") or {}
        organizer = participants.get("organizer") or {}
        organizer_user = (organizer.get("identity") or {}).get("user") or {}
        seller_email = user.mail or organizer.get("upn")
        seller_name = (user.name or organizer_user.get("displayName") or _name_from_alias(seller_email, parsed.speakers)
                       or _name_from_email(seller_email) or "Seller")
        customers: list[Participant] = []
        for number, attendee in enumerate(participants.get("attendees") or [], start=1):
            identity = (attendee.get("identity") or {}).get("user") or {}
            upn = attendee.get("upn") or ""
            email = upn if "@" in upn else None
            if identity.get("id") == user.id or (email and seller_email and email.lower() == seller_email.lower()):
                continue
            name = (identity.get("displayName") or _name_from_alias(email, parsed.speakers) or _name_from_email(email)
                    or f"Attendee {number}")
            customers.append(Participant(id=f"cust{len(customers) + 1}", display_name=name, email=email,
                                         role=SpeakerRole.customer))
        started = _parse_time(meeting.get("startDateTime")) or utcnow()
        duration = float(parsed.meta_hint.get("duration_seconds") or 0) or None
        ended = _parse_time(meeting.get("endDateTime")) or (started + timedelta(seconds=duration) if duration else None)
        meta = ConversationMeta(
            source=Channel.teams, source_interaction_id=transcript_id, direction=Direction.outbound,
            title=meeting.get("subject") or "Teams meeting", started_at=started, ended_at=ended, duration_seconds=duration,
            seller=Participant(id="seller", display_name=seller_name, email=seller_email, role=SpeakerRole.seller),
            participants=customers, market=self.settings.default_market, business_line=self.settings.default_business_line,
            language_hint=parsed.language, consent=ConsentInfo(
                status="recorded_with_consent", basis="Teams transcription notice shown to all participants"))
        return IngestInput(meta=meta, kind="transcript", segments=parsed.segments, speakers=parsed.speakers,
                           transcript_origin=TranscriptOrigin.teams_platform, language=parsed.language)

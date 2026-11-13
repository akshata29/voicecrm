"""The phone line end to end: answer -> notice -> unmixed recording -> ring the seller -> hang up -> the pipeline.

Azure Communication Services is replaced by `FakeTelephony` (every request it receives is recorded), the Call Automation
callbacks and Event Grid events are the documented payloads, and time is a virtual clock the tests advance. The pipeline
behind the line is the real one (rules engine, local PII, local CRM simulator): a call really becomes a reviewable,
CRM-matched proposal.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.adapters.sources import phone as wire
from voicecrm.adapters.telephony.fake import FakeTelephony
from voicecrm.domain.conversation import Segment, utcnow
from voicecrm.domain.enums import Channel, ConversationStatus, Direction, TranscriptOrigin
from voicecrm.pipeline.phone_live import (
    CallerProfile, DirectoryUser, PhoneCall, PhoneLine, PhoneSeller, PhoneTokenRequest)
from voicecrm.ports.telephony import AnsweredCall, Target, TelephonyError
from voicecrm.ports.transcription import NO_SPEECH_MESSAGE, NoSpeechError, TranscriptionRequest, TranscriptionResult

from ..phone_support import (
    CALLER, COGNITIVE, CONSOLE, LINE, PHONE, PUBLIC, RECORDING_HOST, SECRET, SELLER, SELLER_OID, Clock, callback_event,
    incoming_event, make_wav, participants_update, phone_caller, phone_settings, recording_event, recording_metadata, square,
    until)
from .test_llm_pipeline import _service

DANA = CallerProfile(name="Dana Whitfield", org="Harborview Pension Fund", email="dana.whitfield@harborview-pension.example",
                     phone="+1 206 555 0111")
TURNS = [
    (0, "Hello, this is Dana Whitfield from Harborview Pension Fund. I would like to talk about our equity sleeve review."),
    (1, "Hi Dana, thanks for calling. I can send the factsheet today and set up a review next week."),
    (0, "That would be great. Please send it to my email."),
]


class ScriptedTranscriber:
    """Stands in for Azure Speech: a short two-party call, each turn labelled with the speaker of its channel."""

    name = "scripted"

    def __init__(self) -> None:
        self.requests: list[TranscriptionRequest] = []
        self.audio: list[wire.PcmAudio] = []
        self.script = TURNS
        self.fail: Exception | None = None             # raised instead of a transcript
        self.hold: asyncio.Event | None = None         # a slow service: the answer waits until this is set

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        self.audio.append(wire.read_pcm16(Path(request.audio.path).read_bytes()))
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail
        known = request.separate_channels and len(request.channel_speakers) == 2
        who = request.channel_speakers if known else ["S1", "S2"]
        segments = [Segment(id=i, speaker_id=who[side], start_ms=800 + i * 6000, end_ms=5000 + i * 6000, text=text, locale="en-US")
                    for i, (side, text) in enumerate(self.script)]
        return TranscriptionResult(segments=segments, language="en-US", origin=TranscriptOrigin.azure_speech,
                                   audio_seconds=19.0, provider=self.name)


class AcsCall:
    """The Azure Communication Services side of one call: posts the callbacks the service sends as the call progresses."""

    def __init__(self, harness: Harness, cid: str, answered: AnsweredCall) -> None:
        self.h, self.cid = harness, cid
        self.conn, self.server = answered.call_connection_id, answered.server_call_id

    async def post(self, kind: str, **data) -> None:
        event = callback_event(kind, self.conn, server=self.server, **data)
        await self.h.line.handle_callbacks(self.cid, wire.parse_callbacks([event]))

    async def connected(self) -> None:
        await self.post("CallConnected")

    async def notice_played(self) -> None:
        await self.post("PlayCompleted", operationContext="notice")

    async def seller_joins(self) -> None:
        await self.post("AddParticipantSucceeded", operationContext="seller")

    async def seller_unreachable(self, message: str = "no answer") -> None:
        await self.post("AddParticipantFailed", operationContext="seller", resultInformation={"code": 400, "message": message})

    async def participants(self, *raw_ids: str) -> None:
        await self.h.line.handle_callbacks(self.cid, wire.parse_callbacks([participants_update(self.conn, *raw_ids)]))

    async def disconnected(self) -> None:
        await self.post("CallDisconnected")

    @property
    def state(self) -> str | None:
        return self.h.state(self.cid)


class Harness:
    def __init__(self, tmp_path: Path, *, directory=None, **settings) -> None:
        self.tmp, self.directory = tmp_path, directory
        self.pipeline = _service(tmp_path / "svc", FakeLlm(lambda call: {}, model="x"))
        self.service = self.pipeline.service
        self.transcriber = ScriptedTranscriber()
        self.service.deps.transcription = self.transcriber
        self.tel, self.clock = FakeTelephony(), Clock()
        self.settings = phone_settings(tmp_path, **settings)
        self.lines: list[PhoneLine] = []
        self.line = self.new_line()

    def new_line(self) -> PhoneLine:
        line = PhoneLine(self.service, self.tel, settings=self.settings, state_file=self.tmp / "state" / "phone.json",
                         sleep=self.clock.sleep, directory=self.directory)
        self.lines.append(line)
        self.line = line
        return line

    async def close(self) -> None:
        for line in self.lines:
            await line.aclose()

    # -- looking at the line
    def pub(self, cid: str) -> PhoneCall | None:
        status = self.line.status()
        return next((p for p in [*status.active, *status.recent] if p.call_id == cid), None)

    def state(self, cid: str) -> str | None:
        pub = self.pub(cid)
        return pub.state if pub else None

    def conversations(self) -> list:
        return self.pipeline.store.list_conversations()

    # -- driving a call
    async def incoming(self, caller=CALLER, *, correlation: str = "corr-1", **event) -> AcsCall:
        """An IncomingCall event; returns once the line has answered."""
        answered = len(self.tel.answered)
        await self.line.handle_events([incoming_event(caller=caller, correlation=correlation, **event)])
        await until(lambda: len(self.tel.answered) > answered and self.state(correlation) == "connecting", what="the call to be answered")
        return AcsCall(self, correlation, self.tel.answered[-1])

    async def to_ringing(self, call: AcsCall) -> None:
        await call.connected()
        await call.notice_played()

    def stage_recording(self, channels: list[list[int]], layout: list[tuple[str, int | None]], *, doc: str = "doc", chunks: int = 1) -> None:
        for index in range(chunks):
            base = f"{RECORDING_HOST}/{doc}-{index}/content"
            self.tel.downloads[f"{base}/audio"] = make_wav(channels)
            self.tel.downloads[f"{base}/acsmetadata"] = recording_metadata(*layout).encode()

    def recording_event(self, call: AcsCall, **kw) -> dict:
        """The event as the service sends it: the call's correlation id in the subject and a server id of its own."""
        return recording_event(**{"call_id": call.cid, "server": RECORDING_SERVER_ID, **kw})

    async def deliver_until(self, call: AcsCall, channels, layout, state: str, *, doc: str = "doc", chunks: int = 1, **event) -> None:
        """The recording ACS publishes a few minutes after the call: files in its storage, then the Event Grid event."""
        self.stage_recording(channels, layout, doc=doc, chunks=chunks)
        await self.line.handle_events([self.recording_event(call, doc=doc, chunks=chunks, **event)])
        await until(lambda: self.state(call.cid) == state, what=f"the call to be {state}")
        await self.service.drain()

    async def deliver_recording(self, call: AcsCall, channels, layout, *, doc: str = "doc", chunks: int = 1, **event) -> None:
        await self.deliver_until(call, channels, layout, "ingested", doc=doc, chunks=chunks, **event)


CALLER_SPEECH, SELLER_SPEECH = square(3200, 3000), square(3200, 1000)
RECORDING_SERVER_ID = "5f005e80-6ece-42f8-9a61-1744e0d89127"        # a recording event's server id is not the answered call's


@pytest.fixture()
async def make(tmp_path):
    made: list[Harness] = []

    def factory(**kw) -> Harness:
        harness = Harness(tmp_path, **kw)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        await harness.close()


@pytest.fixture()
def seller_console():
    def register(h: Harness) -> None:
        h.line.register_console(CONSOLE, "Aisha West", "aisha.west@contoso.example")
    return register


# ---- the whole call --------------------------------------------------------------------------------------------------
async def test_a_browser_call_is_answered_recorded_and_becomes_a_crm_matched_proposal(make, seller_console):
    h = make()
    seller_console(h)
    token = await h.line.issue_token(PhoneTokenRequest(role="caller", profile=DANA))
    assert token.identity.startswith("8:acs:") and token.line_identity == LINE and token.number == "+1 800 555 0100"

    call = await h.incoming(token.identity)
    ((args, kw),) = h.tel.ops("answer")
    assert args[0] == "ctx-1" and kw["cognitive_endpoint"] == COGNITIVE
    assert args[1].startswith(f"{PUBLIC}/api/v1/webhooks/phone/calls/corr-1?t=")
    assert wire.valid_callback_token(SECRET, "corr-1", args[1].rsplit("t=", 1)[1])

    await call.connected()
    ((args, kw),) = h.tel.ops("play_text")                     # the notice is spoken first ...
    assert args[0] == call.conn and "recorded" in args[1] and kw["context"] == "notice" and call.state == "notice"
    assert not h.tel.ops("start_recording")                    # ... and nothing is recorded before it has played
    await call.notice_played()
    ((_, kw),) = h.tel.ops("start_recording")
    assert kw["channels"] == [Target("acs", token.identity), Target("acs", CONSOLE, "Aisha West")]
    assert kw["state_callback_url"].startswith(f"{PUBLIC}/api/v1/webhooks/phone/calls/corr-1?t=")
    ((args, kw),) = h.tel.ops("add_participant")
    assert args[1] == Target("acs", CONSOLE, "Aisha West") and kw["timeout_seconds"] == 20
    assert kw["display_name"] == "Dana Whitfield" and call.state == "calling_seller"

    await call.seller_joins()
    assert call.state == "in_call"
    await call.participants(token.identity, CONSOLE)
    await call.disconnected()
    ended = h.pub("corr-1")
    assert ended.state == "ended" and ended.duration_seconds is not None and ended.recording == "active"
    await until(lambda: 900 in h.clock.delays(), what="the wait for the recording")     # with a limit

    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(token.identity, 0), (CONSOLE, 1)])
    (rec,) = h.conversations()
    assert rec.status == ConversationStatus.needs_review, rec.error
    assert (rec.meta.source, rec.meta.direction, rec.meta.source_interaction_id) == (Channel.phone, Direction.inbound, "acs-doc-0")
    assert rec.meta.consent.status == "recorded_with_consent" and "notice" in (rec.meta.consent.basis or "")
    assert rec.meta.started_at == ended.connected_at and rec.meta.ended_at == ended.ended_at      # the call's times, not the recording's
    assert (rec.meta.seller.display_name, rec.meta.seller.email) == ("Aisha West", "aisha.west@contoso.example")
    (customer,) = rec.meta.participants
    assert (customer.id, customer.display_name, customer.email, customer.phone, customer.org) == (
        "cust1", "Dana Whitfield", DANA.email, DANA.phone, DANA.org)
    assert rec.proposal.crm_match.account.id == "acc-001"

    (request,) = h.transcriber.requests                        # each party on a channel of their own
    assert request.separate_channels and request.channel_speakers == ["cust1", "seller"]
    (audio,) = h.transcriber.audio
    left, right = wire.channel_levels(audio)
    assert audio.channels == 2 and left > 2 * right > 0         # caller on the left (louder in this recording), seller on the right
    speakers = next(e.detail["speakers"] for e in h.pipeline.store.list_events(rec.meta.conversation_id)
                    if e.stage.value == "normalize" and e.status.value == "completed")
    assert set(speakers) == {"Dana Whitfield", "Aisha West"}

    assert [args[0] for args, _ in h.tel.ops("delete_recording")] == [f"{RECORDING_HOST}/doc-0"]
    status = h.line.status()
    assert (status.calls, status.ingested, status.failed, status.missed, status.active) == (1, 1, 0, 0, [])
    assert status.recent[0].conversation_id == rec.meta.conversation_id and status.recent[0].state == "ingested"
    assert [e.label for e in status.recent[0].timeline][:3] == ["Call received from Dana Whitfield", "Answered the call", "Call connected"]


async def test_a_phone_number_caller_is_matched_to_the_crm_by_the_number(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming(phone_caller(), name=PHONE)
    assert h.pub("corr-1").caller == PHONE and h.pub("corr-1").caller_kind == "phone" and h.pub("corr-1").caller_name is None
    await h.to_ringing(call)
    ((_, kw),) = h.tel.ops("start_recording")
    assert kw["channels"][0] == Target("phone", PHONE)         # the caller's channel is requested by number
    await call.seller_joins()
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(f"4:{PHONE}", 0), (CONSOLE, 1)])
    (rec,) = h.conversations()
    assert rec.status == ConversationStatus.needs_review, rec.error
    (customer,) = rec.meta.participants
    assert (customer.phone, customer.display_name, customer.email) == (PHONE, "+1 206 555 0111", None)
    assert (rec.proposal.crm_match.method, rec.proposal.crm_match.account.id) == ("phone", "acc-001")


async def test_a_teams_seller_is_rung_in_teams_and_named_from_the_directory(make):
    asked: list[str] = []

    async def directory(ident: str) -> DirectoryUser:
        asked.append(ident)
        return DirectoryUser(id=SELLER_OID, name="Aisha West", email="aisha.west@contoso.example")

    h = make(phone_seller="teams:aisha.west@contoso.example", phone_seller_name="", phone_seller_email="", directory=directory)
    call = await h.incoming()
    await h.to_ringing(call)
    ((args, _),) = h.tel.ops("add_participant")
    assert args[1] == Target("teams", SELLER_OID, "Aisha West") and asked == ["aisha.west@contoso.example"]
    ((_, kw),) = h.tel.ops("start_recording")
    assert kw["channels"] == [Target("acs", CALLER), Target("teams", SELLER_OID, "Aisha West")]
    await call.seller_joins()
    await call.participants(CALLER, SELLER)                    # the Teams user appears as 8:orgid:<object id>
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (SELLER, 1)])
    (rec,) = h.conversations()
    assert rec.status == ConversationStatus.needs_review, rec.error
    assert (rec.meta.seller.display_name, rec.meta.seller.email) == ("Aisha West", "aisha.west@contoso.example")
    assert h.transcriber.requests[0].channel_speakers == ["cust1", "seller"]
    assert h.line.status().seller == PhoneSeller(mode="teams", name="Aisha West", online=True)


@pytest.mark.parametrize("seller,directory,rings", [
    (f"teams:{SELLER_OID}", None, Target("teams", SELLER_OID, "Aisha West")),         # an object id needs no directory
    ("teams:aisha@contoso.example", None, None),                                        # a UPN cannot be resolved without one
    ("teams:not-a-person", None, None),
    (f"acs:{CONSOLE}", None, Target("acs", CONSOLE, "Aisha West")),                     # a fixed browser console
])
async def test_the_configured_seller_decides_who_is_rung(make, seller, directory, rings):
    h = make(phone_seller=seller)
    call = await h.incoming()
    await h.to_ringing(call)
    targets = [args[1] for args, _ in h.tel.ops("add_participant")]
    assert targets == ([rings] if rings else [])
    assert call.state == ("calling_seller" if rings else "leaving_message")


# ---- nobody to talk to -----------------------------------------------------------------------------------------------
async def test_when_nobody_answers_the_caller_leaves_a_message_that_is_still_ingested(make, seller_console):
    h = make()
    seller_console(h)
    h.transcriber.script = [TURNS[0]]
    call = await h.incoming()
    await h.to_ringing(call)
    await h.clock.settle()
    assert h.clock.delays() == [25, 1800]                      # the ring limit (+5 s grace) and the maximum call length
    await h.clock.fire(25)
    await until(lambda: call.state == "leaving_message", what="the voicemail offer")
    ((cancelled, _),) = h.tel.ops("cancel_invite")
    assert cancelled[0] == call.conn
    prompts = [(args[1], kw["context"]) for args, kw in h.tel.ops("play_text")]
    assert prompts[-1][1] == "voicemail" and "leave your message" in prompts[-1][0]
    await h.clock.settle()
    assert not h.tel.ops("hang_up") and 60 in h.clock.delays()
    await h.clock.fire(60)                                     # the message length limit
    await until(lambda: h.tel.ops("hang_up"), what="the line to hang up")
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, square(3200, 2500)], [(CALLER, 0), (LINE, 1)])      # channel 1: the line's prompt
    (rec,) = h.conversations()
    assert rec.status != ConversationStatus.failed, rec.error
    request = h.transcriber.requests[0]
    assert request.channel_speakers == ["cust1", "seller"]                   # a lone caller still goes through the channel labels
    audio = h.transcriber.audio[0]
    assert audio.channels == 2 and set(audio.channel(1)) == {0}              # ... with a silent seller channel
    speakers = next(e.detail["speakers"] for e in h.pipeline.store.list_events(rec.meta.conversation_id)
                    if e.stage.value == "normalize" and e.status.value == "completed")
    assert speakers == ["Dana Whitfield"]                                    # nothing is attributed to the seller
    assert h.line.status().ingested == 1


async def test_a_seller_who_cannot_be_reached_sends_the_caller_to_voicemail_at_once(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_unreachable("the device is offline")
    await h.clock.settle()
    assert call.state == "leaving_message" and h.clock.delays() == [60, 1800]     # the ring timer is gone
    assert "the device is offline" in h.pub("corr-1").timeline[-1].label
    await call.seller_joins()                                  # a late success does not undo the voicemail offer
    assert call.state == "in_call"


async def test_without_any_seller_online_the_call_is_recorded_as_a_message(make):
    h = make()
    call = await h.incoming()
    await h.to_ringing(call)
    ((_, kw),) = h.tel.ops("start_recording")
    assert kw["channels"] == [Target("acs", CALLER)] and not h.tel.ops("add_participant")
    assert call.state == "leaving_message"


async def test_the_lines_own_voice_on_its_channel_is_not_part_of_the_voicemail(make):
    h = make()
    call = await h.incoming()
    await h.to_ringing(call)
    prompt = square(3200, 2500)                                # what the line says (the voicemail prompt) lands on its own channel
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, prompt], [(CALLER, 0), (LINE, 1)])
    assert h.transcriber.requests[0].channel_speakers == ["cust1", "seller"]
    audio = h.transcriber.audio[0]
    assert audio.channels == 2 and list(audio.channel(0)) == CALLER_SPEECH and set(audio.channel(1)) == {0}


async def test_the_lines_voice_is_left_out_of_a_mix_too(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    prompt = square(3200, 2500)
    layout = [("8:acs:somebody-else", 0), (LINE, 1), ("8:acs:somebody-new", 2)]          # nobody can be matched, but the line is known
    await h.deliver_recording(call, [CALLER_SPEECH, prompt, SELLER_SPEECH], layout)
    audio = h.transcriber.audio[0]
    assert h.transcriber.requests[0].channel_speakers == [] and audio.channels == 1
    assert list(audio.channel(0)) == [(a + b) // 2 for a, b in zip(CALLER_SPEECH, SELLER_SPEECH, strict=True)]


async def test_a_call_that_cannot_ring_anyone_is_recorded_as_a_message_and_waits(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_unreachable("no answer")
    assert call.state == "leaving_message" and h.tel.ops("play_text")[-1][1]["context"] == "voicemail"


async def test_a_console_that_stopped_checking_in_is_not_rung(make):
    h = make()
    assert h.line.status().seller == PhoneSeller(mode="none", name=None, online=False)
    assert h.line.register_console(CONSOLE, "Aisha West", None) == 45
    assert h.line.status().seller == PhoneSeller(mode="console", name="Aisha West", online=True)
    h.line._consoles[CONSOLE].seen = utcnow() - timedelta(seconds=60)
    assert h.line.status().seller == PhoneSeller(mode="console", name="Aisha West", online=False)
    assert await h.line.pick_seller() is None
    h.line.register_console(CONSOLE, "Aisha West", None)
    assert (await h.line.pick_seller()).target == Target("acs", CONSOLE, "Aisha West")
    h.line.unregister_console(CONSOLE)
    assert h.line.status().seller.mode == "none"


# ---- hanging up ------------------------------------------------------------------------------------------------------
async def test_when_the_caller_hangs_up_the_call_ends_for_everyone(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.participants(CALLER)                            # the seller has not joined yet: nothing to end
    assert not h.tel.ops("hang_up")
    await call.seller_joins()
    await call.participants(CALLER, CONSOLE)
    await call.participants(CONSOLE)                           # the caller is gone
    assert [a[0] for a, _ in h.tel.ops("hang_up")] == [call.conn]
    await call.participants()
    assert len(h.tel.ops("hang_up")) == 1                      # only once


async def test_when_the_seller_hangs_up_the_call_ends_for_everyone(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.participants(CALLER, CONSOLE)
    await call.participants(CALLER)
    assert len(h.tel.ops("hang_up")) == 1 and "seller hung up" in h.pub("corr-1").timeline[-1].label


async def test_a_caller_who_hangs_up_before_the_recording_starts_is_a_missed_call(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await call.connected()
    await call.disconnected()
    pub = h.pub("corr-1")
    assert pub.state == "missed" and pub.recording == "none" and pub.error is None
    assert (h.line.status().missed, h.line.status().ingested) == (1, 0)
    assert not h.tel.ops("start_recording") and h.clock.delays() == []


async def test_the_maximum_call_length_hangs_up(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await h.clock.fire(1800)
    await until(lambda: h.tel.ops("hang_up"), what="the line to hang up")
    assert "Maximum call length" in h.pub("corr-1").timeline[-1].label


async def test_a_recording_that_never_arrives_is_reported_and_a_late_one_still_counts(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    await h.clock.fire(900)
    await until(lambda: call.state == "failed", what="the watchdog")
    assert "no recording arrived" in h.pub("corr-1").error and h.line.status().failed == 1
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    assert call.state == "ingested" and h.pub("corr-1").error is None                      # the late recording is not lost
    status = h.line.status()
    assert (status.ingested, status.failed) == (1, 0) and len(h.conversations()) == 1


# ---- the recording notice --------------------------------------------------------------------------------------------
async def test_a_notice_that_cannot_be_played_ends_the_call_when_it_is_required(make, seller_console):
    h = make()
    seller_console(h)
    h.tel.fail["play_text"] = TelephonyError("the speech resource refused the request")
    call = await h.incoming()
    await call.connected()
    assert call.state == "failed" and "recording notice" in h.pub("corr-1").error
    assert [a[0] for a, _ in h.tel.ops("hang_up")] == [call.conn] and not h.tel.ops("start_recording")


@pytest.mark.parametrize("settings,expected", [
    ({"phone_require_notice": False}, "failed"),                                   # the prompt errors out
    ({"phone_require_notice": False, "phone_cognitive_endpoint": ""}, "skipped"),   # no voice connection at all
])
async def test_the_call_can_be_recorded_without_the_notice_but_not_claim_consent(make, seller_console, settings, expected):
    h = make(**settings)
    seller_console(h)
    h.tel.fail["play_text"] = TelephonyError("the speech resource refused the request")
    call = await h.incoming()
    await h.to_ringing(call) if expected == "skipped" else await call.connected()
    assert h.pub("corr-1").notice == expected and h.tel.ops("start_recording")
    await call.seller_joins()
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    (rec,) = h.conversations()
    assert rec.meta.consent.status == "unknown" and "not confirmed" in rec.meta.consent.basis


async def test_a_notice_that_never_finishes_is_given_up_on(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await call.connected()
    await h.clock.settle()
    assert 45 in h.clock.delays()
    await h.clock.fire(45)
    await until(lambda: call.state == "failed", what="the notice timeout")
    assert h.tel.ops("hang_up") and not h.tel.ops("start_recording")


async def test_without_a_voice_connection_and_with_the_notice_required_the_call_is_not_recorded(make, seller_console):
    h = make(phone_cognitive_endpoint="")
    seller_console(h)
    call = await h.incoming()
    await call.connected()
    assert call.state == "failed" and not h.tel.ops("play_text") and h.tel.ops("hang_up")
    assert h.line.status().notice_enabled is False


# ---- failures at the service ------------------------------------------------------------------------------------------
async def test_a_call_that_cannot_be_answered_is_missed_with_the_reason(make):
    h = make()
    h.tel.fail["answer"] = TelephonyError("the call context expired", status=400)
    await h.line.handle_events([incoming_event()])
    await until(lambda: h.state("corr-1") == "missed", what="the failed answer")
    assert h.pub("corr-1").error == "the call context expired" and h.line.status().last_error == "the call context expired"
    assert h.line.status().missed == 1


async def test_a_recording_that_cannot_be_started_hangs_up(make, seller_console):
    h = make()
    seller_console(h)
    h.tel.fail["start_recording"] = TelephonyError("recording is not allowed")
    call = await h.incoming()
    await h.to_ringing(call)
    pub = h.pub("corr-1")
    assert pub.state == "failed" and pub.recording == "failed" and not h.tel.ops("add_participant") and h.tel.ops("hang_up")


async def test_a_failure_while_handling_a_callback_fails_the_call_instead_of_the_server(make, seller_console):
    h = make()
    seller_console(h)
    h.tel.fail["add_participant"] = TelephonyError("the target cannot be invited")
    call = await h.incoming()
    await h.to_ringing(call)                                   # an invitation error means voicemail, not a failed call
    assert call.state == "leaving_message"
    h.tel.fail["hang_up"] = TelephonyError("already over")
    await call.participants(CALLER)                            # hang-up errors are swallowed
    assert call.state == "leaving_message"


# ---- who and what is let in ------------------------------------------------------------------------------------------
async def test_the_same_incoming_call_is_answered_once(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.line.handle_events([incoming_event(event_id="evt-retry"), incoming_event(event_id="evt-retry-2")])
    await asyncio.sleep(0.05)
    assert len(h.tel.ops("answer")) == 1 and h.line.status().calls == 1
    await call.connected()
    await call.disconnected()
    await h.line.handle_events([incoming_event(event_id="evt-late")])                   # a late redelivery after the call ended
    await asyncio.sleep(0.05)
    assert len(h.tel.ops("answer")) == 1


async def test_calls_to_other_identities_are_ignored_unless_they_are_added(make):
    h = make(phone_accept_to=" 28:orgid:00000000-0000-0000-0000-000000000042 , ")
    await h.line.handle_events([incoming_event(to="8:acs:somebody-else", correlation="other")])
    await asyncio.sleep(0.05)
    assert not h.tel.ops("answer") and h.line.status().calls == 0
    call = await h.incoming(to="28:orgid:00000000-0000-0000-0000-000000000042", correlation="resource-account")
    assert call.state == "connecting"


async def test_the_same_recording_is_ingested_once(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    downloads = len(h.tel.ops("download"))
    await h.line.handle_events([h.recording_event(call, event_id="evt-rec-again")])
    await asyncio.sleep(0.05)
    await h.service.drain()
    assert len(h.tel.ops("download")) == downloads and len(h.conversations()) == 1 and h.line.status().ingested == 1


async def test_a_recording_event_pointing_outside_the_recording_service_is_refused(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    await h.line.handle_events([h.recording_event(call, host="https://attacker.example.com/v1/objects")])
    await until(lambda: call.state == "failed", what="the refusal")
    assert "outside the recording service" in h.pub("corr-1").error
    assert not h.tel.ops("download") and not h.tel.ops("delete_recording") and h.conversations() == []


async def test_a_recording_of_a_call_the_line_does_not_remember_is_still_ingested(make):
    h = make()
    ghost = AcsCall(h, "call-1", AnsweredCall("conn-x", "server-unknown"))
    await h.deliver_recording(ghost, [CALLER_SPEECH, SELLER_SPEECH], [("8:acs:unknown-caller", 0), ("8:acs:unknown-seller", 1)],
                              event_id="evt-restart")
    ghost_call = h.line.status().recent[0]
    assert ghost_call.caller == "Unknown caller" and ghost_call.state == "ingested" and ghost_call.recording == "ready"
    assert len(h.line.status().recent) == 1                                                 # listed once
    (rec,) = h.conversations()
    assert rec.meta.source == Channel.phone and h.transcriber.requests[0].channel_speakers == []      # the voices are separated by diarization
    assert rec.meta.consent.status == "unknown"
    assert (rec.meta.started_at.year, rec.meta.duration_seconds) == (2026, 187.0)                     # no call to ask: the recording's


async def test_a_recording_belongs_to_the_call_whose_id_is_in_its_subject(make, seller_console):
    h = make()
    seller_console(h)
    first = await h.incoming(correlation="corr-a")
    second = await h.incoming(correlation="corr-b")
    for call in (first, second):
        await h.to_ringing(call)
        await call.seller_joins()
        await call.disconnected()
    await h.deliver_recording(second, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)], doc="doc-b")
    assert (h.state("corr-a"), h.state("corr-b")) == ("ended", "ingested")                  # not the other call that was waiting too
    assert h.pub("corr-b").seller == "Aisha West" and h.conversations()[0].meta.consent.status == "recorded_with_consent"


async def test_the_server_call_id_is_a_second_chance_to_find_the_call(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    h.stage_recording([CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    await h.line.handle_events([recording_event(call_id="an-id-nobody-knows", server=call.server)])
    await until(lambda: h.state(call.cid) == "ingested", what="the recording to be ingested")
    assert len(h.line.status().recent) == 1


async def test_the_only_call_waiting_for_its_recording_gets_one_nothing_else_claims(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    h.stage_recording([CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    await h.line.handle_events([recording_event(call_id="an-id-nobody-knows", server="neither-do-we")])
    await until(lambda: h.state(call.cid) == "ingested", what="the recording to be ingested")
    await h.service.drain()
    assert len(h.line.status().recent) == 1 and h.conversations()[0].meta.consent.status == "recorded_with_consent"


async def test_with_several_calls_waiting_an_unidentified_recording_is_given_to_none_of_them(make, seller_console):
    h = make()
    seller_console(h)
    waiting = [await h.incoming(correlation=name) for name in ("corr-a", "corr-b")]
    for call in waiting:
        await h.to_ringing(call)
        await call.seller_joins()
        await call.disconnected()
    h.stage_recording([CALLER_SPEECH, SELLER_SPEECH], [("8:acs:someone", 0), ("8:acs:someone-else", 1)])
    await h.line.handle_events([recording_event(call_id="an-id-nobody-knows", server="neither-do-we")])
    await until(lambda: h.line.status().ingested == 1, what="the unidentified recording")
    assert (h.state("corr-a"), h.state("corr-b")) == ("ended", "ended") and h.line.status().calls == 3


async def test_the_chunks_of_a_long_recording_are_joined(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)], chunks=3)
    (audio,) = h.transcriber.audio
    assert audio.channels == 2 and audio.frames == 3 * 3200
    assert len(h.tel.ops("delete_recording")) == 3


async def test_a_recording_the_pipeline_cannot_read_fails_the_call_and_can_be_retried(make, seller_console):
    h = make()
    seller_console(h)
    call = await h.incoming()
    await h.to_ringing(call)
    await call.seller_joins()
    await call.disconnected()
    base = f"{RECORDING_HOST}/doc-0/content"
    h.tel.downloads[f"{base}/audio"] = b"this is not a wav file"
    await h.line.handle_events([h.recording_event(call)])
    await until(lambda: call.state == "failed", what="the failure")
    assert "not a WAV" in h.pub("corr-1").error and h.line.status().failed == 1 and h.conversations() == []
    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])      # the same recording, redelivered
    assert call.state == "ingested" and len(h.conversations()) == 1


async def test_a_callback_for_a_call_nobody_remembers_is_ignored(make):
    h = make()
    await h.line.handle_callbacks("nobody", wire.parse_callbacks([callback_event("CallConnected", "conn-9")]))
    assert h.line.status().calls == 0


# ---- recordings with nothing in them ----------------------------------------------------------------------------------
SECOND = 16000
HISS = square(3 * SECOND, 12)                                  # an open microphone in a quiet room, nobody talking
MESSAGE = square(3 * SECOND, 2000)                             # three seconds of speech-level audio


async def hung_up_after_the_notice(h: Harness, seller_online: bool = False) -> AcsCall:
    call = await h.incoming()
    await h.to_ringing(call)
    if seller_online:
        await call.seller_joins()
    await call.disconnected()
    return call


async def test_a_hang_up_with_nothing_said_is_a_missed_call_that_is_not_sent_on(make):
    h = make(phone_min_speech_seconds=1.5)
    call = await hung_up_after_the_notice(h)                   # nobody to answer: the caller is offered voicemail and says nothing
    # the caller's channel is room noise; the line's own prompt is loud on its channel and must not count as speech
    await h.deliver_until(call, [HISS, MESSAGE], [(CALLER, 0), (LINE, 1)], "missed")
    pub = h.pub("corr-1")
    assert pub.error is None and pub.recording == "ready" and pub.conversation_id is None
    assert "No speech in the recording" in pub.timeline[-1].label and "nothing was sent to the pipeline" in pub.timeline[-1].label
    assert h.conversations() == [] and h.transcriber.requests == []
    assert [args[0] for args, _ in h.tel.ops("delete_recording")] == [f"{RECORDING_HOST}/doc-0"]      # the temporary copy goes either way
    status = h.line.status()
    assert (status.calls, status.missed, status.ingested, status.failed, status.last_error) == (1, 1, 0, 0, None)


@pytest.mark.parametrize("floor,speaking,state", [(1.5, 0.5, "missed"), (1.5, 3.0, "ingested"), (5.0, 3.0, "missed"), (0.0, 0.0, "ingested")])
async def test_how_much_speech_a_recording_needs_is_a_setting(make, floor, speaking, state):
    h = make(phone_min_speech_seconds=floor)
    call = await hung_up_after_the_notice(h)
    caller = (square(round(speaking * SECOND), 2000) + HISS) if speaking else HISS
    await h.deliver_until(call, [caller, [0] * len(caller)], [(CALLER, 0), (LINE, 1)], state)
    assert bool(h.conversations()) == (state == "ingested")                # 0 sends every recording on, silent or not


async def test_speech_from_either_party_is_enough(make, seller_console):
    h = make(phone_min_speech_seconds=1.5)
    seller_console(h)
    call = await hung_up_after_the_notice(h, seller_online=True)
    await h.deliver_until(call, [HISS, MESSAGE], [(CALLER, 0), (CONSOLE, 1)], "ingested")          # only the seller spoke
    assert len(h.conversations()) == 1


# ---- what the pipeline made of it ------------------------------------------------------------------------------------
async def test_a_call_waits_for_the_pipeline_before_it_counts_as_ingested(make, seller_console):
    h = make()
    seller_console(h)
    h.transcriber.hold = asyncio.Event()                       # a slow speech service
    call = await hung_up_after_the_notice(h, seller_online=True)
    h.stage_recording([CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    await h.line.handle_events([h.recording_event(call)])
    await until(lambda: h.transcriber.requests, what="the pipeline to start transcribing")
    pub = h.pub("corr-1")
    assert pub.state == "processing" and pub.conversation_id and "waiting for the result" in pub.timeline[-1].label
    assert h.line.status().ingested == 0
    assert [args[0] for args, _ in h.tel.ops("delete_recording")] == [f"{RECORDING_HOST}/doc-0"]      # the audio is the pipeline's now
    h.transcriber.hold.set()
    await until(lambda: h.state("corr-1") == "ingested", what="the call to be ingested")
    assert h.line.status().ingested == 1 and h.conversations()[0].status != ConversationStatus.failed


async def test_a_pipeline_failure_is_what_the_call_reports(make, seller_console):
    h = make()
    seller_console(h)
    h.transcriber.fail = RuntimeError("the speech service is down")
    call = await hung_up_after_the_notice(h, seller_online=True)
    await h.deliver_until(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)], "failed")
    pub = h.pub("corr-1")
    assert "RuntimeError: the speech service is down" in pub.error and pub.conversation_id
    (rec,) = h.conversations()
    assert rec.status == ConversationStatus.failed
    status = h.line.status()
    assert (status.failed, status.ingested) == (1, 0) and "speech service is down" in status.last_error


@pytest.mark.parametrize("how", ["the service finds no language", "the service returns no words"])
async def test_speech_the_service_cannot_recognise_is_a_missed_call_not_a_fault(make, seller_console, how):
    h = make()
    seller_console(h)
    if how.endswith("language"):
        h.transcriber.fail = NoSpeechError()
    else:
        h.transcriber.script = []
    call = await hung_up_after_the_notice(h, seller_online=True)
    await h.deliver_until(call, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)], "missed")
    pub = h.pub("corr-1")
    assert pub.error is None and "no speech" in pub.timeline[-1].label and pub.conversation_id      # linked to the failed row
    (rec,) = h.conversations()
    assert rec.status == ConversationStatus.failed and rec.error == NO_SPEECH_MESSAGE
    status = h.line.status()
    assert (status.missed, status.failed, status.ingested, status.last_error) == (1, 0, 0, None)


async def test_a_call_still_being_processed_when_the_app_stops_is_not_left_processing(make, seller_console):
    h = make()
    seller_console(h)
    h.transcriber.hold = asyncio.Event()
    call = await hung_up_after_the_notice(h, seller_online=True)
    h.stage_recording([CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    await h.line.handle_events([h.recording_event(call)])
    await until(lambda: h.state("corr-1") == "processing" and h.transcriber.requests, what="the pipeline to be at work")
    (remembered,) = h.new_line().status().recent               # the process is gone; the pipeline run it waited for went with it
    assert remembered.state == "failed" and "restarted" in remembered.error
    h.transcriber.hold.set()
    await h.service.drain()


# ---- restarts --------------------------------------------------------------------------------------------------------
async def test_the_line_identity_and_recent_calls_survive_a_restart(make, seller_console):
    h = make(phone_line_identity="")
    await h.line.start()
    line_identity = h.line.status().line_identity
    assert line_identity and line_identity.startswith("8:acs:")
    token = await h.line.issue_token(PhoneTokenRequest(role="caller", profile=DANA))
    seller_console(h)
    call = await h.incoming(token.identity, to=line_identity)
    await h.to_ringing(call)
    await call.seller_joins()

    restarted = h.new_line()                                   # the process is gone; the files are not
    status = restarted.status()
    assert status.line_identity == line_identity and status.active == []
    (remembered,) = status.recent
    assert remembered.state == "ended" and "restarted" in remembered.error

    await h.deliver_recording(call, [CALLER_SPEECH, SELLER_SPEECH], [(token.identity, 0), (CONSOLE, 1)])
    (rec,) = h.conversations()
    assert rec.meta.seller.display_name == "Aisha West" and rec.meta.participants[0].email == DANA.email
    assert restarted.status().ingested == 1 and h.tel.ops("issue_token")[0][0] == (None,)


async def test_a_call_that_was_ringing_when_the_app_stopped_is_marked_missed(make):
    h = make()
    call = await h.incoming()
    assert call.state == "connecting"
    restarted = h.new_line()
    (remembered,) = restarted.status().recent
    assert remembered.state == "missed" and "restarted" in remembered.error


# ---- the demo reset --------------------------------------------------------------------------------------------------
async def test_a_demo_reset_forgets_finished_calls_and_nothing_else(make, seller_console):
    h = make()
    seller_console(h)
    done = await h.incoming(correlation="corr-done")
    await h.to_ringing(done)
    await done.seller_joins()
    await done.disconnected()
    await h.deliver_recording(done, [CALLER_SPEECH, SELLER_SPEECH], [(CALLER, 0), (CONSOLE, 1)])
    live = await h.incoming(correlation="corr-live")
    before = h.line.status()
    assert (before.calls, before.ingested) == (2, 1) and before.last_event_at is not None

    h.line.forget()
    status = h.line.status()
    assert [p.call_id for p in status.active] == ["corr-live"] and status.recent == []
    assert (status.calls, status.ingested, status.failed, status.missed) == (1, 0, 0, 0)
    assert status.last_event_at is None and status.last_error is None
    assert status.line_identity == LINE and status.seller.online                           # the line and the console stay

    downloads = len(h.tel.ops("download"))                     # events already handled stay handled: a redelivery changes nothing
    await h.line.handle_events([incoming_event(correlation="corr-done"), h.recording_event(done, event_id="evt-rec-again")])
    await asyncio.sleep(0.05)
    assert h.line.status().calls == 1 and len(h.tel.ops("download")) == downloads and len(h.conversations()) == 1

    await live.connected()                                     # the call in progress carries on
    assert live.state == "notice"
    assert [p.call_id for p in h.new_line().status().recent] == ["corr-live"]              # and the forgetting was saved


# ---- browser tokens --------------------------------------------------------------------------------------------------
async def test_browser_tokens_are_issued_for_callers_and_sellers_but_never_for_the_line(make):
    h = make()
    caller = await h.line.issue_token(PhoneTokenRequest(role="caller", display_name="Dana", profile=DANA))
    assert (caller.display_name, caller.token, caller.line_identity) == ("Dana", "fake-token", LINE)
    refreshed = await h.line.issue_token(PhoneTokenRequest(role="caller", identity=caller.identity))
    assert refreshed.identity == caller.identity and refreshed.display_name == "Customer"
    assert h.line._profiles[wire.party_key(caller.identity)] == DANA                      # a refresh keeps the profile
    seller = await h.line.issue_token(PhoneTokenRequest(role="seller"))
    assert seller.display_name == "Aisha West" and seller.identity not in (caller.identity, LINE)
    for bad in (LINE, LINE.upper(), SELLER, "not-an-identity"):
        with pytest.raises(ValueError, match="cannot be used"):
            await h.line.issue_token(PhoneTokenRequest(role="seller", identity=bad))


async def test_a_line_with_no_connection_cannot_hand_out_tokens_and_says_why(tmp_path):
    line = PhoneLine(None, None, settings=phone_settings(tmp_path), problem="VOICECRM_PHONE_ACS_ENDPOINT is not set")
    with pytest.raises(TelephonyError, match="ACS_ENDPOINT"):
        await line.issue_token(PhoneTokenRequest(role="caller"))
    status = line.status()
    assert (status.mode, status.configured, status.problem) == ("acs", False, "VOICECRM_PHONE_ACS_ENDPOINT is not set")
    await line.start()                                         # nothing to prepare, nothing raised
    await line.aclose()


async def test_the_status_describes_the_line(make):
    h = make()
    status = h.line.status()
    assert (status.mode, status.configured, status.number, status.line_identity) == ("acs", True, "+1 800 555 0100", LINE)
    assert (status.public_url, status.notice_enabled, status.ingress_listening) == (PUBLIC, True, False)
    assert (status.calls, status.active, status.recent, status.last_error) == (0, [], [], None)
    h.line.ingress_listening = True
    assert h.line.status().ingress_listening is True


# ---- the self-check ---------------------------------------------------------------------------------------------------
HEALTH = f"{PUBLIC}/api/v1/webhooks/phone/health"


async def checks(h: Harness) -> dict[str, tuple[str, str, str]]:
    return {name: (status, detail, hint) for name, status, detail, hint in await h.line.checks()}


async def test_the_self_check_passes_when_everything_is_in_place(make, seller_console):
    h = make()
    seller_console(h)
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        result = await checks(h)
    assert {k: v[0] for k, v in result.items()} == {
        "phone: ACS access": "pass", "phone: public URL": "pass", "phone: seller": "pass", "phone: recording notice": "pass"}
    assert LINE[-4:] in result["phone: ACS access"][1] and LINE not in result["phone: ACS access"][1]       # masked


async def test_the_self_check_explains_each_problem(make):
    h = make(phone_line_identity="", phone_public_base_url="", phone_cognitive_endpoint="")
    h.tel.fail["issue_token"] = TelephonyError("the identity API refused the credential")
    result = await checks(h)
    assert result["phone: ACS access"][0] == "fail" and "refused the credential" in result["phone: ACS access"][1]
    assert result["phone: public URL"][0] == "fail" and "PUBLIC_BASE_URL" in result["phone: public URL"][1]
    assert result["phone: seller"][0] == "warn" and "voicemail" in result["phone: seller"][1]
    assert result["phone: recording notice"][0] == "fail" and "REQUIRE_NOTICE" in result["phone: recording notice"][2]
    relaxed = make(phone_cognitive_endpoint="", phone_require_notice=False)
    assert (await checks(relaxed))["phone: recording notice"][0] == "warn"


@pytest.mark.parametrize("response,fragment", [
    (httpx.Response(200, json={"service": "something-else"}), "not the phone ingress"),
    (httpx.Response(404), "HTTP 404"),
    (httpx.ConnectError("refused"), "cannot reach"),
])
async def test_the_self_check_tells_a_wrong_tunnel_from_a_dead_one(make, response, fragment):
    h = make()
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=response) if isinstance(response, httpx.Response) else router.get(HEALTH).mock(side_effect=response)
        status, detail, hint = (await checks(h))["phone: public URL"]
    assert status == "fail" and fragment in detail and hint


async def test_the_teams_seller_check_resolves_the_user(make):
    async def directory(ident: str) -> DirectoryUser | None:
        return DirectoryUser(id=SELLER_OID, name="Aisha West") if ident == "aisha.west@contoso.example" else None

    h = make(phone_seller="teams:aisha.west@contoso.example", directory=directory)
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        assert (await checks(h))["phone: seller"][:2] == ("pass", "rings Aisha West in Teams")
    nobody = make(phone_seller="teams:nobody@contoso.example", directory=directory)
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        assert (await checks(nobody))["phone: seller"][0] == "fail"

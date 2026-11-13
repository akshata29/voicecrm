"""Shared builders for the phone-line tests: ACS payloads (Event Grid and Call Automation callbacks), unmixed WAV files, settings
and a virtual clock."""
from __future__ import annotations

import asyncio
import json
import struct
from array import array
from pathlib import Path

from voicecrm.adapters.sources import phone as wire
from voicecrm.config import Settings

LINE = "8:acs:11111111-2222-3333-4444-555555555555_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CALLER = "8:acs:11111111-2222-3333-4444-555555555555_99999999-8888-7777-6666-555555555555"
CONSOLE = "8:acs:11111111-2222-3333-4444-555555555555_77777777-6666-5555-4444-333333333333"
SELLER_OID = "cfed8bc2-630a-4d0a-97da-0f5dc4159357"
SELLER = f"8:orgid:{SELLER_OID}"
PHONE = "+12065550111"
RECORDING_HOST = "https://storage.asm.skype.com/v1/objects"
SECRET = "unit-test-secret-0123456789abcdef"
COGNITIVE = "https://ai-test.cognitiveservices.azure.com/"
PUBLIC = "https://line.example.test"


def phone_settings(tmp_path: Path, **kw) -> Settings:
    """A fully configured phone line (every URL is a placeholder; nothing here is reachable)."""
    base = dict(
        _env_file=None, state_dir=tmp_path / "state", phone_mode="acs", phone_ingress_port=0,
        phone_acs_endpoint="https://acs-test.unitedstates.communication.azure.com", phone_public_base_url=PUBLIC,
        phone_webhook_secret=SECRET, phone_display_number="+1 800 555 0100", phone_line_identity=LINE,
        phone_cognitive_endpoint=COGNITIVE, phone_seller_name="Aisha West", phone_seller_email="aisha.west@contoso.example",
        phone_ring_seconds=20, phone_max_call_minutes=30, phone_voicemail_seconds=60,
        phone_min_speech_seconds=0.0)                  # the flow tests use a fraction of a second of audio; the gate has tests of its own
    return Settings(**{**base, **kw})


async def until(predicate, *, what: str = "the expected state", timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


class Clock:
    """Stands in for ``asyncio.sleep``: a timer waits here until the test fires it."""

    def __init__(self) -> None:
        self.timers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.timers.append((delay, future))
        try:
            await future
        finally:
            self.timers = [t for t in self.timers if t[1] is not future]

    def delays(self) -> list[float]:
        return sorted(d for d, f in self.timers if not f.done())

    async def settle(self) -> None:
        """Lets the timers the line has just started reach their first sleep."""
        for _ in range(5):
            await asyncio.sleep(0)

    async def fire(self, delay: float) -> None:
        await until(lambda: any(d == delay and not f.done() for d, f in self.timers), what=f"a {delay} s timer to be waiting")
        for d, future in list(self.timers):
            if d == delay and not future.done():
                future.set_result(None)


def incoming_event(*, to: str = LINE, caller: str | dict = CALLER, correlation: str = "corr-1", context: str = "ctx-1",
                   name: str | None = "Dana Whitfield", event_id: str | None = "evt-1", server: str = "server-0") -> dict:
    sender = caller if isinstance(caller, dict) else {"kind": "communicationUser", "rawId": caller, "communicationUser": {"id": caller}}
    event = {
        "topic": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Communication/communicationServices/acs",
        "subject": "/caller/x/recipient/y", "eventType": "Microsoft.Communication.IncomingCall", "dataVersion": "1.0",
        "data": {"to": {"kind": "communicationUser", "rawId": to, "communicationUser": {"id": to}}, "from": sender,
                 "serverCallId": server, "callerDisplayName": name, "incomingCallContext": context,
                 "correlationId": correlation},
    }
    if event_id:
        event["id"] = event_id
    return event


def phone_caller(number: str = PHONE) -> dict:
    return {"kind": "phoneNumber", "rawId": f"4:{number}", "phoneNumber": {"value": number}}


def recording_event(*, chunks: int = 1, server: str = "srv-1", recording: str = "rec-1", event_id: str = "evt-rec",
                    host: str = RECORDING_HOST, doc: str = "doc", call_id: str = "call-1") -> dict:
    return {
        "id": event_id, "eventType": "Microsoft.Communication.RecordingFileStatusUpdated",
        "subject": f"/recording/call/{call_id}/serverCallId/{server}/recordingId/{recording}",
        "data": {
            "recordingStorageInfo": {"recordingChunks": [
                {"documentId": f"{doc}-{i}", "index": i, "endReason": "SessionEnded",
                 "metadataLocation": f"{host}/{doc}-{i}/content/acsmetadata",
                 "contentLocation": f"{host}/{doc}-{i}/content/audio",
                 "deleteLocation": f"{host}/{doc}-{i}"} for i in reversed(range(chunks))]},
            "recordingStartTime": "2026-11-10T15:00:00.1234567Z", "recordingDurationMs": 187_000, "sessionEndReason": "CallEnded"},
    }


def callback_event(kind: str, conn: str, *, server: str | None = None, **data) -> dict:
    """A Call Automation callback as ACS posts it (a CloudEvent); ``data`` adds operationContext, participants ..."""
    body = {"callConnectionId": conn, **data}
    if server:
        body["serverCallId"] = server
    return {"type": f"Microsoft.Communication.{kind}", "data": body}


def participants_update(conn: str, *raw_ids: str) -> dict:
    return callback_event("ParticipantsUpdated", conn, participants=[
        {"identifier": {"rawId": raw, "kind": "phoneNumber" if raw.startswith("4:") else "communicationUser"}} for raw in raw_ids])


def recording_metadata(*channels: tuple[str, int | None], sample_rate: int = 16000) -> str:
    """The metadata file next to an unmixed recording: who is on which channel."""
    return json.dumps({
        "resourceId": "res", "callId": "call-1", "chunkDocumentId": "doc-0", "chunkIndex": 0,
        "chunkStartTime": "2026-11-10T15:00:00Z", "chunkDuration": 187000,
        "recordingInfo": {"contentType": "audio", "channelType": "unmixed", "format": "wav",
                          "audioConfiguration": {"sampleRate": sample_rate, "bitRate": 256000, "channels": len(channels)}},
        "participants": [{"participantId": pid, **({"channel": ch} if ch is not None else {})} for pid, ch in channels]})


# ---- audio -----------------------------------------------------------------------------------------------------------
def square(frames: int, amplitude: int) -> list[int]:
    return [amplitude if (i // 8) % 2 == 0 else -amplitude for i in range(frames)]


def _interleave(channels: list[list[int]]) -> array:
    interleaved = array("h", bytes(len(channels[0]) * len(channels) * 2))
    for index, samples in enumerate(channels):
        interleaved[index::len(channels)] = array("h", samples)
    return interleaved


def make_wav(channels: list[list[int]], rate: int = 16000) -> bytes:
    return wire.write_wav(_interleave(channels), len(channels), rate)


def extensible_wav(channels: list[list[int]], rate: int = 16000, data_size: int | None = None) -> bytes:
    """A WAVE_FORMAT_EXTENSIBLE file (what multi-channel recordings use), with a LIST chunk before the data."""
    count = len(channels)
    pcm = _interleave(channels).tobytes()
    guid = struct.pack("<H", 1) + bytes.fromhex("000000001000800000aa00389b71")            # KSDATAFORMAT_SUBTYPE_PCM
    fmt = struct.pack("<HHIIHHHHI", 0xFFFE, count, rate, rate * count * 2, count * 2, 16, 22, 16, 0) + guid
    listing = b"LIST" + struct.pack("<I", 5) + b"INFOx" + b"\x00"                             # odd size: padded to even
    size = len(pcm) if data_size is None else data_size
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + listing + b"data" + struct.pack("<I", size) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body

"""The phone line's wire formats: Event Grid and Call Automation payloads, recording metadata and unmixed WAV audio.

Everything here is pure (no network, no clock), so the call state machine in pipeline/phone_live.py is tested with plain
dicts and the formats are written down in one place.

* Event Grid delivers ``IncomingCall`` and ``RecordingFileStatusUpdated`` as a JSON array in the Event Grid schema, after
  a one-off ``SubscriptionValidationEvent`` handshake.
* Call Automation posts its callbacks (``CallConnected``, ``PlayCompleted``, ``AddParticipantSucceeded`` ...) to the
  ``callback_url`` given when the call is answered, as CloudEvents.
* A recording is a WAV file plus a metadata JSON that says which participant is on which channel. An *unmixed* recording
  has one channel per participant, which is what lets the transcript attribute every word to the caller or the seller
  without guessing.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import struct
import sys
import wave
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal
from urllib.parse import urlparse

from ...domain.base import VModel

EVENT_VALIDATION = "Microsoft.EventGrid.SubscriptionValidationEvent"
EVENT_INCOMING_CALL = "Microsoft.Communication.IncomingCall"
EVENT_RECORDING_READY = "Microsoft.Communication.RecordingFileStatusUpdated"

# Recording downloads carry the service's credentials, so only the service's own storage hosts are ever fetched.
RECORDING_HOST_SUFFIXES = (".asm.skype.com", ".asyncgw.teams.microsoft.com")
SILENCE_RMS = 25.0                         # a channel quieter than this (16-bit scale) holds no speech
SPEECH_RMS = 100.0                         # a 100 ms stretch louder than this is speech-level: the room noise of a quiet call sits near 10-20
SPEECH_WINDOW_SECONDS = 0.1


# ---- who is on the call ----------------------------------------------------------------------------------------------
PartyKind = Literal["phone", "acs", "teams", "app", "unknown"]


class Party(VModel):
    """An ACS communication identifier as it appears in event payloads."""

    kind: PartyKind = "unknown"
    raw_id: str = ""
    phone: str | None = None                   # E.164, for phone parties
    display_name: str | None = None


def _kind_of(raw: str, hint: Any) -> PartyKind:
    value = raw.casefold()
    if value.startswith("4:"):
        return "phone"
    if value.startswith("8:acs:") or value.startswith("8:spool:"):
        return "acs"
    if value.startswith(("8:orgid:", "8:dod:", "8:gcch:")):
        return "teams"
    if value.startswith("28:"):
        return "app"
    return {"phoneNumber": "phone", "communicationUser": "acs", "microsoftTeamsUser": "teams",
            "microsoftTeamsApp": "app"}.get(str(hint), "unknown")


def parse_party(value: Any, *, display_name: str | None = None) -> Party:
    """A ``CommunicationIdentifier`` JSON object (or just a raw id) as a `Party`."""
    if isinstance(value, str):
        value = {"rawId": value}
    if not isinstance(value, dict):
        return Party()

    def inner(key: str, field: str) -> str | None:
        obj = value.get(key)
        return str(obj[field]) if isinstance(obj, dict) and obj.get(field) else None

    phone = inner("phoneNumber", "value")
    acs = inner("communicationUser", "id")
    teams = inner("microsoftTeamsUser", "userId")
    raw = str(value.get("rawId") or "").strip() or (f"4:{phone}" if phone else acs or (f"8:orgid:{teams}" if teams else ""))
    kind = _kind_of(raw, value.get("kind"))
    if kind == "phone" and not phone:
        phone = raw[2:] if raw.startswith("4:") else raw
    return Party(kind=kind, raw_id=raw, phone=phone if kind == "phone" else None, display_name=display_name)


def party_key(raw: str) -> str:
    """Comparable form of an identity: phone numbers by digits, users by the id after the last colon."""
    value = raw.strip().casefold()
    if value.startswith("4:"):
        return "tel:" + re.sub(r"\D", "", value[2:])
    if value.startswith("+") or re.fullmatch(r"[\d\s()+-]{7,}", value):
        return "tel:" + re.sub(r"\D", "", value)
    return value.rsplit(":", 1)[-1]


def same_party(a: str | None, b: str | None) -> bool:
    """True when two raw ids name the same person (``8:orgid:<guid>`` and ``<guid>`` are the same Teams user)."""
    if not a or not b:
        return False
    key = party_key(a)
    return bool(key) and key == party_key(b)


def mask(value: str | None) -> str:
    """A phone number or id for logs: the last four characters only."""
    if not value:
        return "-"
    return ("*" * max(0, len(value) - 4)) + value[-4:]


def display_number(number: str | None) -> str | None:
    """+12065550100 -> '+1 206 555 0100' (North American numbers); anything else unchanged."""
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return number


# ---- Event Grid ------------------------------------------------------------------------------------------------------
def as_events(body: Any) -> list[dict[str, Any]]:
    """A webhook body as a list of event objects (Event Grid sends arrays; a CloudEvent may arrive alone)."""
    if isinstance(body, dict):
        return [body]
    if isinstance(body, list):
        return [e for e in body if isinstance(e, dict)]
    return []


def event_type(event: dict[str, Any]) -> str:
    return str(event.get("eventType") or event.get("type") or "")


def validation_code(events: Iterable[dict[str, Any]]) -> str | None:
    """The code to echo back for Event Grid's subscription handshake, or None when this is not a handshake."""
    for event in events:
        if event_type(event) == EVENT_VALIDATION:
            code = (event.get("data") or {}).get("validationCode")
            if code:
                return str(code)
    return None


class IncomingCall(VModel):
    event_id: str | None = None
    correlation_id: str
    server_call_id: str | None = None
    incoming_call_context: str
    caller: Party
    callee: Party
    caller_name: str | None = None
    custom_context: dict[str, Any] = {}


def parse_incoming_call(event: dict[str, Any]) -> IncomingCall | None:
    if event_type(event) != EVENT_INCOMING_CALL:
        return None
    data = event.get("data")
    if not isinstance(data, dict) or not data.get("incomingCallContext"):
        return None
    name = data.get("callerDisplayName")
    correlation = str(data.get("correlationId") or event.get("id") or "")
    if not correlation:
        return None
    custom = data.get("customContext")
    return IncomingCall(
        event_id=str(event["id"]) if event.get("id") else None, correlation_id=correlation,
        server_call_id=str(data["serverCallId"]) if data.get("serverCallId") else None,
        incoming_call_context=str(data["incomingCallContext"]), caller=parse_party(data.get("from"), display_name=name),
        callee=parse_party(data.get("to")), caller_name=str(name) if name else None,
        custom_context=custom if isinstance(custom, dict) else {})


class RecordingChunk(VModel):
    index: int = 0
    document_id: str | None = None
    content_location: str
    metadata_location: str | None = None
    delete_location: str | None = None
    end_reason: str | None = None


class RecordingReady(VModel):
    event_id: str | None = None
    call_id: str | None = None
    server_call_id: str | None = None
    recording_id: str | None = None
    started_at: datetime | None = None
    duration_ms: int | None = None
    end_reason: str | None = None
    chunks: list[RecordingChunk] = []

    @property
    def key(self) -> str:
        """Stable identity of this recording, used as the pipeline's idempotency key."""
        first = self.chunks[0].document_id if self.chunks else None
        if first:
            return f"acs-{first}"
        basis = f"{self.server_call_id}|{self.recording_id}|{self.started_at}"
        return "acs-" + hashlib.sha256(basis.encode()).hexdigest()[:32]


_SUBJECT = re.compile(r"/recording/call/(?P<call>[^/]+)/serverCallId/(?P<server>[^/]+)/recordingId/(?P<rec>[^/]+)")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)         # .NET timestamps carry 7 fractional digits
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_recording_ready(event: dict[str, Any]) -> RecordingReady | None:
    if event_type(event) != EVENT_RECORDING_READY:
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    info = data.get("recordingStorageInfo") if isinstance(data.get("recordingStorageInfo"), dict) else {}
    chunks: list[RecordingChunk] = []
    for raw in info.get("recordingChunks") or []:
        if isinstance(raw, dict) and raw.get("contentLocation"):
            chunks.append(RecordingChunk(
                index=int(raw.get("index") or 0), document_id=str(raw["documentId"]) if raw.get("documentId") else None,
                content_location=str(raw["contentLocation"]),
                metadata_location=str(raw["metadataLocation"]) if raw.get("metadataLocation") else None,
                delete_location=str(raw["deleteLocation"]) if raw.get("deleteLocation") else None,
                end_reason=str(raw["endReason"]) if raw.get("endReason") else None))
    if not chunks:
        return None
    match = _SUBJECT.search(str(event.get("subject") or ""))
    duration = data.get("recordingDurationMs")
    return RecordingReady(
        event_id=str(event["id"]) if event.get("id") else None,
        call_id=match.group("call") if match else None, server_call_id=match.group("server") if match else None,
        recording_id=match.group("rec") if match else None, started_at=parse_time(data.get("recordingStartTime")),
        duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
        end_reason=str(data["sessionEndReason"]) if data.get("sessionEndReason") else None,
        chunks=sorted(chunks, key=lambda c: c.index))


# ---- Call Automation callbacks ---------------------------------------------------------------------------------------
class Callback(VModel):
    kind: str                                  # CallConnected, PlayCompleted, AddParticipantFailed, ...
    call_connection_id: str | None = None
    server_call_id: str | None = None
    correlation_id: str | None = None
    operation_context: str | None = None
    result_code: int | None = None
    result_message: str | None = None
    participants: list[Party] = []             # ParticipantsUpdated: everyone on the call now
    participant: Party | None = None           # AddParticipantSucceeded / Failed: the invitee
    recording_state: str | None = None         # RecordingStateChanged: active | inactive
    recording_id: str | None = None


def parse_callbacks(body: Any) -> list[Callback]:
    out: list[Callback] = []
    for event in as_events(body):
        kind = event_type(event).rsplit(".", 1)[-1]
        data = event.get("data")
        if not kind or not isinstance(data, dict):
            continue
        result = data.get("resultInformation") if isinstance(data.get("resultInformation"), dict) else {}
        listed = [item.get("identifier", item) if isinstance(item, dict) else item for item in data.get("participants") or []]
        code = result.get("code")
        out.append(Callback(
            kind=kind, call_connection_id=data.get("callConnectionId"), server_call_id=data.get("serverCallId"),
            correlation_id=data.get("correlationId"), operation_context=data.get("operationContext"),
            result_code=int(code) if isinstance(code, (int, float)) else None,
            result_message=str(result["message"]) if result.get("message") else None,
            participants=[parse_party(p) for p in listed],
            participant=parse_party(data["participant"]) if data.get("participant") else None,
            recording_state=str(data["recordingState"]).lower() if data.get("recordingState") else None,
            recording_id=data.get("recordingId")))
    return out


# ---- webhook secrets -------------------------------------------------------------------------------------------------
def callback_token(secret: str, call_id: str) -> str:
    """The token in a call's callback URL: only someone who knows the secret can mint a valid URL for a call id."""
    return hmac.new(secret.encode(), f"phone-callback:{call_id}".encode(), hashlib.sha256).hexdigest()[:40]


def valid_callback_token(secret: str, call_id: str, token: str | None) -> bool:
    return bool(secret and token) and hmac.compare_digest(callback_token(secret, call_id).encode(), str(token).encode())


def valid_event_key(secret: str, key: str | None) -> bool:
    return bool(secret and key) and hmac.compare_digest(secret.encode(), str(key).encode())


def is_recording_url(url: str | None) -> bool:
    """True for an https URL on the recording service's own hosts (no credentials, no odd ports, no look-alikes)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return (parsed.scheme == "https" and "@" not in parsed.netloc and parsed.port in (None, 443) and host.isascii()
            and any(host.endswith(suffix) for suffix in RECORDING_HOST_SUFFIXES))


# ---- recording metadata ----------------------------------------------------------------------------------------------
class MetaParticipant(VModel):
    participant_id: str
    channel: int | None = None


class RecordingMetadata(VModel):
    call_id: str | None = None
    chunk_index: int | None = None
    chunk_start: datetime | None = None
    chunk_duration_ms: int | None = None
    channel_type: str | None = None
    audio_format: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    participants: list[MetaParticipant] = []


def parse_recording_metadata(raw: bytes | str | dict[str, Any]) -> RecordingMetadata:
    import json

    obj: Any = raw
    if isinstance(raw, (bytes, bytearray)):
        obj = json.loads(bytes(raw).decode("utf-8-sig"))
    elif isinstance(raw, str):
        obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("recording metadata is not a JSON object")
    info = obj.get("recordingInfo") if isinstance(obj.get("recordingInfo"), dict) else {}
    audio = info.get("audioConfiguration") if isinstance(info.get("audioConfiguration"), dict) else {}
    people = []
    for item in obj.get("participants") or []:
        if isinstance(item, dict) and item.get("participantId"):
            channel = item.get("channel")
            people.append(MetaParticipant(participant_id=str(item["participantId"]),
                                          channel=int(channel) if isinstance(channel, (int, float)) else None))
    index, duration = obj.get("chunkIndex"), obj.get("chunkDuration")
    return RecordingMetadata(
        call_id=str(obj["callId"]) if obj.get("callId") else None,
        chunk_index=int(index) if isinstance(index, (int, float)) else None, chunk_start=parse_time(obj.get("chunkStartTime")),
        chunk_duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
        channel_type=str(info["channelType"]).lower() if info.get("channelType") else None,
        audio_format=str(info["format"]).lower() if info.get("format") else None,
        sample_rate=int(audio["sampleRate"]) if isinstance(audio.get("sampleRate"), (int, float)) else None,
        channels=int(audio["channels"]) if isinstance(audio.get("channels"), (int, float)) else None, participants=people)


# ---- audio -----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PcmAudio:
    """Interleaved 16-bit PCM."""

    channels: int
    rate: int
    samples: array

    @property
    def frames(self) -> int:
        return len(self.samples) // self.channels

    def channel(self, index: int) -> array:
        return self.samples[index::self.channels]


def read_pcm16(data: bytes) -> PcmAudio:
    """Parses a 16-bit PCM WAV, including WAVE_FORMAT_EXTENSIBLE (multi-channel recordings) and unfinalised data sizes."""
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise ValueError("the recording is not a WAV file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag == 0xFFFE and size >= 26 and body + 26 <= len(data):
                tag = struct.unpack_from("<H", data, body + 24)[0]            # the sub-format GUID starts with the real tag
            fmt = (tag, channels, rate, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("the WAV file has no format chunk")
            tag, channels, rate, bits = fmt
            if tag != 1 or bits != 16 or channels < 1:
                raise ValueError(f"only 16-bit PCM recordings are supported (format {tag}, {bits} bits)")
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            raw = data[body:end]
            raw = raw[:len(raw) - len(raw) % (channels * 2)]
            samples = array("h")
            samples.frombytes(raw)
            if sys.byteorder == "big":
                samples.byteswap()
            return PcmAudio(channels=channels, rate=rate, samples=samples)
        pos = body + size + (size & 1)
    raise ValueError("the WAV file contains no audio")


def write_wav(samples: array, channels: int, rate: int) -> bytes:
    out = array("h", samples)
    if sys.byteorder == "big":
        out.byteswap()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(out.tobytes())
    return buffer.getvalue()


def channel_levels(pcm: PcmAudio) -> list[float]:
    """RMS level of every channel (sampled: this only has to tell speech from silence)."""
    levels = []
    for index in range(pcm.channels):
        sample = pcm.channel(index)[::16]
        levels.append(math.sqrt(sum(x * x for x in sample) / len(sample)) if sample else 0.0)
    return levels


ChannelMode = Literal["stereo", "caller_only", "mono_mix"]


@dataclass(frozen=True)
class ChannelPlan:
    """How to turn a recording's channels into what the transcriber gets."""

    mode: ChannelMode
    caller_channel: int | None = None
    seller_channel: int | None = None
    note: str = ""
    mix_channels: tuple[int, ...] = ()          # mono_mix: the channels that carry people's speech (never the line's own)


def plan_channels(meta: RecordingMetadata, levels: Sequence[float], *, caller_id: str | None,
                  seller_ids: Sequence[str], line_ids: Sequence[str] = ()) -> ChannelPlan:
    """Chooses stereo (caller left, seller right), the caller alone, or a mix of every channel that carries speech.

    The metadata names the participant on each channel, so the caller and the seller are found by identity. The line's own
    identity has a channel too (its spoken prompts land there), and that is never part of the conversation. When an identity
    cannot be matched the plan degrades instead of guessing who is who: whatever is left over is mixed to mono and the
    transcriber separates the voices itself.
    """
    count = len(levels)
    by_channel = {p.channel: p.participant_id for p in meta.participants if p.channel is not None and 0 <= p.channel < count}
    caller = next((c for c, pid in by_channel.items() if same_party(pid, caller_id)), None)
    seller = next((c for c, pid in by_channel.items() if any(same_party(pid, sid) for sid in seller_ids)), None)
    line = {c for c, pid in by_channel.items() if any(same_party(pid, lid) for lid in line_ids)} - {caller, seller}
    active = [c for c in range(count) if levels[c] >= SILENCE_RMS and c not in line]
    if caller is not None and seller is not None and caller != seller:
        return ChannelPlan("stereo", caller, seller, "caller and seller identified by their channels")
    if caller is not None and not [c for c in active if c != caller]:
        return ChannelPlan("caller_only", caller, None, "only the caller spoke")
    if seller is not None and caller is None:
        others = [c for c in active if c != seller]
        if len(others) == 1:
            return ChannelPlan("stereo", others[0], seller, "the caller is the one other channel with speech")
    return ChannelPlan("mono_mix", note="participants could not be matched to channels; mixed to mono", mix_channels=tuple(active))


def render_plan(pcm: PcmAudio, plan: ChannelPlan) -> bytes:
    """A WAV for the plan: 2 channels (caller, seller; the seller's silent for a caller alone), or mono."""
    frames = pcm.frames
    if plan.mode in ("stereo", "caller_only") and plan.caller_channel is not None:
        out = array("h", bytes(frames * 4))
        out[0::2] = pcm.channel(plan.caller_channel)
        if plan.seller_channel is not None:
            out[1::2] = pcm.channel(plan.seller_channel)
        return write_wav(out, 2, pcm.rate)
    loud = plan_sources(pcm, plan)
    mixed = pcm.channel(loud[0])
    for c in loud[1:]:
        mixed = array("h", ((a + b) // 2 for a, b in zip(mixed, pcm.channel(c), strict=True)))
    return write_wav(mixed, 1, pcm.rate)


def plan_sources(pcm: PcmAudio, plan: ChannelPlan) -> list[int]:
    """The recording's channels that carry the conversation under a plan (never the line's own prompts)."""
    if plan.mode in ("stereo", "caller_only") and plan.caller_channel is not None:
        return [plan.caller_channel] + ([plan.seller_channel] if plan.seller_channel is not None else [])
    levels = channel_levels(pcm)
    return list(plan.mix_channels) or [c for c in range(pcm.channels) if levels[c] >= SILENCE_RMS] or [0]


def speech_seconds(pcm: PcmAudio, channels: Sequence[int], *, stop_at: float | None = None) -> float:
    """Seconds of speech-level audio: the 100 ms stretches in which any of ``channels`` is louder than ``SPEECH_RMS``.

    This only has to tell speech from silence and noise: a caller who hangs up, or leaves nothing on a voicemail, produces
    seconds of room noise and perhaps a click. It stops counting at ``stop_at`` seconds (speech shows up at once, silence has
    to be read to the end), and it samples every fourth value, which is plenty for a level.
    """
    window = max(1, round(pcm.rate * SPEECH_WINDOW_SECONDS))
    streams = [pcm.channel(c) for c in channels if 0 <= c < pcm.channels]
    limit = None if stop_at is None else max(1, math.ceil(stop_at / SPEECH_WINDOW_SECONDS - 1e-9))
    loud = 0
    for start in range(0, pcm.frames, window):
        for stream in streams:
            sample = stream[start:start + window:4]
            if sample and math.sqrt(sum(x * x for x in sample) / len(sample)) > SPEECH_RMS:
                loud += 1
                break
        if limit is not None and loud >= limit:
            break
    return loud * SPEECH_WINDOW_SECONDS


def concat_wavs(parts: Sequence[bytes]) -> bytes:
    """Joins WAVs of one format end to end (the chunks of a long recording)."""
    audio = [read_pcm16(p) for p in parts]
    first = audio[0]
    if any(a.channels != first.channels or a.rate != first.rate for a in audio):
        raise ValueError("recording chunks differ in format")
    joined = array("h")
    for a in audio:
        joined.extend(a.samples)
    return write_wav(joined, first.channels, first.rate)

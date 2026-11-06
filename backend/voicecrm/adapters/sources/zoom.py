"""Zoom: webhook helpers, cloud-recording files, and transcript parsing / rendering.

Webhooks (Zoom docs): the first call is an `endpoint.url_validation` challenge answered with
`{"plainToken", "encryptedToken"}` (HMAC-SHA256 of the plain token with the webhook secret token, hex). Every event is
signed: `x-zm-signature = "v0=" + hex(HMAC-SHA256(secret, "v0:<x-zm-request-timestamp>:<raw body>"))` - verify against
the raw request body, not re-serialised JSON.

Recordings: `recording.completed` lists `recording_files` (M4A audio_only, TRANSCRIPT = a VTT transcript when audio
transcription is enabled, ...) and a top-level `download_token` (valid 24 h) that is sent as a Bearer token with the
file's `download_url`. The same shape arrives on the other recording events that carry a download token.

Transcript formats handled: the cloud-recording VTT (`Speaker Name: text` cues) and a JSON `timeline` transcript
(`ts`, `end_ts`, `text`, `users[]` - the shape of Zoom Phone's recording transcript download). The audio-first design in
the requirements prefers the M4A; the transcript is used when the customer already has it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from ...domain.conversation import Segment
from ...domain.fixtures import ScriptedCall
from .common import (
    UNKNOWN_SPEAKER, parse_started_at, participants_by_id, speaker_label, turn_timings,
)
from .mobile import SAMPLE_RATE, audio_seconds
from .vtt import format_timestamp, iter_cues, parse_timestamp

logger = logging.getLogger(__name__)

ZOOM_HOSTS = ("zoom.us", "zoom.com")
SIGNATURE_TOLERANCE_SECONDS = 300
_SPEAKER_RE = re.compile(r"^\s*(?P<name>[^:\n<>]{1,60}?):\s+(?P<text>\S.*)$", re.DOTALL)
_SPEAKER_MODE_RATIO = 0.6


class ZoomError(RuntimeError):
    pass


# ---- webhook ---------------------------------------------------------------------------------------------------------
def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def url_validation_response(plain_token: str, secret: str) -> dict[str, str]:
    """Answer to the `endpoint.url_validation` challenge (respond within 3 seconds with 200)."""
    return {"plainToken": plain_token, "encryptedToken": _hmac_hex(secret, plain_token.encode("utf-8"))}


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def sign_request(body: bytes | str, secret: str, *, timestamp: int | None = None) -> dict[str, str]:
    """Headers Zoom would send for `body` (used by the simulator and tests)."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    signature = "v0=" + _hmac_hex(secret, f"v0:{stamp}:".encode() + raw)
    return {"x-zm-request-timestamp": str(stamp), "x-zm-signature": signature}


def verify_signature(
    headers: Mapping[str, str], body: bytes | str, secret: str, *,
    tolerance_seconds: int | None = SIGNATURE_TOLERANCE_SECONDS, now: float | None = None,
) -> bool:
    """True when `x-zm-signature` matches the raw body (and, by default, the timestamp is within 5 minutes)."""
    lowered = _lower(headers)
    stamp, signature = lowered.get("x-zm-request-timestamp"), lowered.get("x-zm-signature")
    if not stamp or not signature:
        return False
    if tolerance_seconds is not None:
        try:
            if abs((time.time() if now is None else now) - int(stamp)) > tolerance_seconds:
                return False
        except ValueError:
            return False
    raw = body.encode("utf-8") if isinstance(body, str) else body
    expected = "v0=" + _hmac_hex(secret, f"v0:{stamp}:".encode() + raw)
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8"))


# ---- recording files ---------------------------------------------------------------------------------------------------
class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RecordingFile(_Loose):
    id: str | None = None
    meeting_id: str | None = None
    recording_start: str | None = None
    recording_end: str | None = None
    file_type: str = ""                            # M4A | MP4 | TRANSCRIPT | CC | CHAT | TIMELINE | SUMMARY ...
    file_extension: str | None = None
    file_size: int | None = None
    recording_type: str | None = None              # audio_only | audio_transcript | closed_caption ...
    status: str | None = None
    download_url: str
    play_url: str | None = None

    @property
    def is_audio(self) -> bool:
        return self.file_type.upper() == "M4A" or self.recording_type == "audio_only"

    @property
    def is_transcript(self) -> bool:
        return self.file_type.upper() == "TRANSCRIPT" or self.recording_type == "audio_transcript"


class ZoomRecording(_Loose):
    event: str | None = None
    meeting_id: str | None = None
    uuid: str | None = None
    topic: str | None = None
    host_email: str | None = None
    start_time: str | None = None
    duration_minutes: int | None = None
    download_token: str | None = None
    files: list[RecordingFile] = []


def _recording_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = payload.get("payload")
    for candidate in (inner.get("object") if isinstance(inner, Mapping) else None, payload.get("object"), payload):
        if isinstance(candidate, Mapping) and "recording_files" in candidate:
            return candidate
    return {}


def download_token_of(payload: Mapping[str, Any]) -> str | None:
    inner = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {}
    for source in (payload, inner):
        for key in ("download_token", "download_access_token"):
            if source.get(key):
                return str(source[key])
    return None


def recording_files(payload: Mapping[str, Any]) -> list[RecordingFile]:
    """Files listed by a recording webhook (`recording.completed` and the other events with the same shape)."""
    obj = _recording_object(payload)
    files = obj.get("recording_files") if obj else None
    out: list[RecordingFile] = []
    for entry in files if isinstance(files, list) else []:
        if isinstance(entry, Mapping) and entry.get("download_url"):
            out.append(RecordingFile.model_validate(dict(entry)))
    return out


def recording_info(payload: Mapping[str, Any]) -> ZoomRecording:
    obj = _recording_object(payload)
    return ZoomRecording(
        event=payload.get("event"), meeting_id=str(obj["id"]) if obj.get("id") is not None else None,
        uuid=obj.get("uuid"), topic=obj.get("topic"), host_email=obj.get("host_email"),
        start_time=obj.get("start_time"), duration_minutes=obj.get("duration"),
        download_token=download_token_of(payload), files=recording_files(payload),
    )


def _host_allowed(url: str, allowed_hosts: tuple[str, ...] | None) -> bool:
    if allowed_hosts is None:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


async def download_recording_file(
    client: httpx.AsyncClient, file: RecordingFile, download_token: str, *,
    allowed_hosts: tuple[str, ...] | None = ZOOM_HOSTS,
) -> bytes:
    """GET the file with `Authorization: Bearer <download_token>` (the webhook's token, or an OAuth access token).

    Only Zoom hosts are fetched by default (the URL comes from a webhook body); pass `allowed_hosts=None` for
    simulated payloads that point at this application.
    """
    if not _host_allowed(file.download_url, allowed_hosts):
        raise ZoomError(
            f"download host {urlsplit(file.download_url).hostname!r} is not allowed (allowed_hosts={allowed_hosts})"
        )
    response = await client.get(
        file.download_url, headers={"Authorization": f"Bearer {download_token}"}, follow_redirects=True
    )
    if response.status_code != 200:
        raise ZoomError(f"Zoom download failed with HTTP {response.status_code}")
    return response.content


# ---- transcript parsing ------------------------------------------------------------------------------------------------
def _split_speaker(text: str) -> tuple[str | None, str]:
    match = _SPEAKER_RE.match(text)
    if not match or len(match.group("name").split()) > 6 or match.group("name").strip().isdigit():
        return None, text
    return match.group("name").strip(), match.group("text").strip()


def parse_zoom_vtt(text: str) -> list[Segment]:
    """Zoom cloud-recording VTT (`Speaker Name: text`). Files without speaker names get speaker 'Unknown'."""
    cues = iter_cues(text)
    flattened = [re.sub(r"\s+", " ", cue.payload).strip() for cue in cues]
    split = [_split_speaker(t) for t in flattened]
    speaker_mode = bool(cues) and sum(1 for s, _ in split if s) / len(cues) >= _SPEAKER_MODE_RATIO
    segments: list[Segment] = []
    previous = UNKNOWN_SPEAKER
    for cue, original, (speaker, spoken) in zip(cues, flattened, split, strict=True):
        if speaker_mode:
            previous = speaker or previous
            label, content = previous, spoken
        else:
            label, content = UNKNOWN_SPEAKER, original
        if content:
            segments.append(
                Segment(id=len(segments), speaker_id=label, start_ms=cue.start_ms, end_ms=cue.end_ms, text=content)
            )
    return segments


def _millis(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    try:
        return parse_timestamp(str(value))
    except (ValueError, IndexError):
        return None


def _timeline_speaker(entry: Mapping[str, Any]) -> str:
    users = entry.get("users")
    first = users[0] if isinstance(users, list) and users and isinstance(users[0], Mapping) else {}
    for source in (first, entry):
        for key in ("username", "user_name", "display_name", "name", "speaker"):
            if source.get(key):
                return str(source[key]).strip()
    for source in (first, entry):
        for key in ("userId", "user_id", "zoom_userid"):
            if source.get(key) not in (None, ""):
                return str(source[key])
    ids = entry.get("userIds")
    if isinstance(ids, list) and ids:
        return str(ids[0])
    if entry.get("channelMark"):
        return f"channel-{entry['channelMark']}"
    return UNKNOWN_SPEAKER


def _as_object(data: Any) -> Any:
    if isinstance(data, (bytes, str)):
        return json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    return data


def parse_zoom_transcript_json(data: Any) -> list[Segment]:
    """Zoom JSON transcript with a `timeline` array -> segments (speaker_id = the user name, else the user id)."""
    obj = _as_object(data)
    timeline = obj.get("timeline") if isinstance(obj, Mapping) else obj
    segments: list[Segment] = []
    for entry in timeline if isinstance(timeline, list) else []:
        if not isinstance(entry, Mapping):
            continue
        text = re.sub(r"\s+", " ", str(entry.get("text") or entry.get("raw_text") or "")).strip()
        if not text:
            continue
        start = _millis(entry.get("ts")) or 0
        end = _millis(entry.get("end_ts"))
        segments.append(Segment(id=len(segments), speaker_id=_timeline_speaker(entry), start_ms=start,
                                end_ms=end if end is not None and end >= start else start, text=text))
    return segments


def zoom_json_meta(data: Any) -> dict[str, Any]:
    """Interaction id and meeting facts carried by a Zoom JSON transcript, for the pipeline's metadata hint."""
    obj = _as_object(data)
    if not isinstance(obj, Mapping):
        return {}
    meta: dict[str, Any] = {}
    interaction = obj.get("meeting_id") or obj.get("recording_id") or obj.get("uuid")
    if interaction:
        meta["source_interaction_id"] = str(interaction)
    if obj.get("meeting_topic") or obj.get("topic"):
        meta["title"] = obj.get("meeting_topic") or obj.get("topic")
    start, end = obj.get("recording_start"), obj.get("recording_end")
    if start:
        meta["started_at"] = start
    try:
        if start and end:
            started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            meta["duration_seconds"] = (ended - started).total_seconds()
    except ValueError:
        pass
    return meta


# ---- rendering -----------------------------------------------------------------------------------------------------------
def render_zoom_vtt(call: ScriptedCall) -> str:
    """The call as Zoom's cloud-recording transcript: numbered cues, `Speaker Name: text`."""
    people = participants_by_id(call)
    lines = ["WEBVTT", ""]
    for number, (turn, (start, end)) in enumerate(zip(call.turns, turn_timings(call), strict=True), start=1):
        text = re.sub(r"\s*\n\s*", " ", turn.text).strip()
        lines += [str(number), f"{format_timestamp(start)} --> {format_timestamp(end)}",
                  f"{speaker_label(call, turn, people)}: {text}", ""]
    return "\n".join(lines)


def render_zoom_transcript_json(call: ScriptedCall) -> dict[str, Any]:
    """The call as a Zoom JSON transcript (`timeline` entries with ts, end_ts, text and users)."""
    people = participants_by_id(call)
    started = parse_started_at(call)
    ended = started + timedelta(minutes=call.duration_minutes)
    timeline = [
        {"ts": format_timestamp(start), "end_ts": format_timestamp(end), "text": turn.text,
         "users": [{"username": speaker_label(call, turn, people)}]}
        for turn, (start, end) in zip(call.turns, turn_timings(call), strict=True)
    ]
    return {
        "ver": 1, "meeting_id": _meeting_uuid(call), "meeting_topic": call.title,
        "recording_start": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "recording_end": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeline": timeline,
    }


def _meeting_uuid(call: ScriptedCall) -> str:
    return f"zoom-{call.fixture_id}"


def render_recording_completed_payload(call: ScriptedCall, base_url: str) -> dict[str, Any]:
    """A `recording.completed` webhook body for the call: an M4A audio file and a TRANSCRIPT (VTT) file.

    download_url = `<base_url>/<fixture_id>/m4a` and `<base_url>/<fixture_id>/transcript`. The simulated audio is the
    fixture's silent WAV (the pipeline identifies it by content, not by the M4A label). Not signed: use `sign_request`.
    """
    base = base_url.rstrip("/")
    started = parse_started_at(call)
    stamp = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    ended = (started + timedelta(minutes=call.duration_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    meeting_uuid = _meeting_uuid(call)
    transcript_size = len(render_zoom_vtt(call).encode("utf-8"))

    def file_entry(kind: str, extension: str, recording_type: str, size: int, slug: str) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicecrm:zoom-file:{call.fixture_id}:{kind}")),
            "meeting_id": meeting_uuid, "recording_start": stamp, "recording_end": ended, "file_type": kind,
            "file_extension": extension, "file_size": size, "play_url": f"{base}/{call.fixture_id}/play/{slug}",
            "download_url": f"{base}/{call.fixture_id}/{slug}", "status": "completed", "recording_type": recording_type,
        }

    files = [
        file_entry("M4A", "M4A", "audio_only", 44 + int(SAMPLE_RATE * audio_seconds(call)) * 2, "m4a"),
        file_entry("TRANSCRIPT", "VTT", "audio_transcript", transcript_size, "transcript"),
    ]
    return {
        "event": "recording.completed",
        "event_ts": int((started + timedelta(minutes=call.duration_minutes)).timestamp() * 1000),
        "payload": {
            "account_id": "SYNTHETIC-ACCOUNT",
            "object": {
                "uuid": meeting_uuid, "id": int(uuid.uuid5(uuid.NAMESPACE_URL, meeting_uuid).int % 10**10),
                "host_id": "synthetic-host", "host_email": call.seller.email, "topic": call.title, "type": 2,
                "start_time": stamp, "timezone": "UTC", "duration": call.duration_minutes,
                "total_size": sum(f["file_size"] for f in files), "recording_count": len(files),
                "share_url": f"{base}/{call.fixture_id}/share", "recording_files": files,
            },
        },
        "download_token": "synthetic-download-token",
    }

"""Mobile / in-person capture: audio uploads with an optional JSON sidecar, and audio sniffing.

A phone (or the installable web app) records locally - possibly offline - and uploads the audio later together with a
small sidecar describing the capture. Sniffing looks at the file's magic bytes, not its name, because browsers and OS
recorders disagree about extensions (Safari records audio/mp4, Chrome audio/webm;codecs=opus).

Sidecar JSON (all keys optional; nested `seller` / `client` objects or the flat `seller_name` / `client_org` ...):
  capture_id, seller{name,email}, client{name,org,email,phone}, captured_at (ISO-8601), duration_seconds, offline,
  language_hint (BCP-47), title
"""
from __future__ import annotations

import io
import json
import logging
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel, ConfigDict

from ...domain.fixtures import ScriptedCall
from .common import parse_started_at

logger = logging.getLogger(__name__)

SAMPLE_RATE = 8000
MAX_SILENCE_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AudioInfo:
    format: str            # wav | mp3 | m4a | ogg | webm
    content_type: str
    extension: str


WAV = AudioInfo("wav", "audio/wav", ".wav")
MP3 = AudioInfo("mp3", "audio/mpeg", ".mp3")
M4A = AudioInfo("m4a", "audio/mp4", ".m4a")
OGG = AudioInfo("ogg", "audio/ogg", ".ogg")
WEBM = AudioInfo("webm", "audio/webm", ".webm")

_BY_EXTENSION = {
    ".wav": WAV, ".wave": WAV, ".mp3": MP3, ".m4a": M4A, ".mp4": M4A, ".ogg": OGG, ".oga": OGG, ".opus": OGG,
    ".webm": WEBM,
}
_BY_CONTENT_TYPE = {
    "audio/wav": WAV, "audio/x-wav": WAV, "audio/wave": WAV, "audio/vnd.wave": WAV, "audio/mpeg": MP3, "audio/mp3": MP3,
    "audio/mp4": M4A, "audio/x-m4a": M4A, "audio/m4a": M4A, "audio/aac": M4A, "audio/ogg": OGG, "audio/opus": OGG,
    "audio/webm": WEBM, "video/webm": WEBM,
}


def sniff_audio(data: bytes, filename: str | None = None) -> AudioInfo | None:
    """Identify wav / mp3 / m4a / ogg / webm from the leading bytes; None when they are not recognised.

    `filename` is only used to break the tie for ISO-BMFF files (an `.mp4` upload is treated as m4a audio).
    """
    head = bytes(data[:16])
    if len(head) < 4:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return WAV
    if head[:4] == b"OggS":
        return OGG
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return WEBM
    if head[4:8] == b"ftyp":
        return M4A
    if head[:3] == b"ID3":
        return MP3
    if _is_mp3_frame(head):
        return MP3
    return None


def _is_mp3_frame(head: bytes) -> bool:
    """MPEG audio frame header: 11 sync bits, a real version and layer, a valid bitrate and sample-rate index."""
    if head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
        return False
    version, layer = (head[1] >> 3) & 0x03, (head[1] >> 1) & 0x03
    bitrate, sample_rate = (head[2] >> 4) & 0x0F, (head[2] >> 2) & 0x03
    return version != 1 and layer != 0 and bitrate not in (0, 0x0F) and sample_rate != 3


def audio_from_hints(filename: str | None, content_type: str | None) -> AudioInfo | None:
    """Fallback when the bytes are not recognised: trust the declared content type, then the extension."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared in _BY_CONTENT_TYPE:
        return _BY_CONTENT_TYPE[declared]
    return _BY_EXTENSION.get(PurePath(filename or "").suffix.lower())


# ---- silent audio for fixtures ------------------------------------------------------------------------------------------
def audio_seconds(call: ScriptedCall) -> float:
    """Length of the fixture's silent WAV: about the call length, capped at 30 s to keep it small."""
    return float(min(MAX_SILENCE_SECONDS, max(1.0, call.duration_minutes * 60)))


def silent_wav(seconds: float, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A valid 16-bit mono PCM WAV of silence."""
    frames = max(1, int(sample_rate * seconds))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


# ---- sidecar ------------------------------------------------------------------------------------------------------------
class MobileSidecar(BaseModel):
    """Capture metadata sent next to a mobile recording. Unknown keys are ignored (clients evolve)."""

    model_config = ConfigDict(extra="ignore")

    capture_id: str | None = None
    seller_name: str | None = None
    seller_email: str | None = None
    client_name: str | None = None
    client_org: str | None = None
    client_email: str | None = None
    client_phone: str | None = None
    captured_at: datetime | None = None
    duration_seconds: float | None = None
    offline: bool = False
    language_hint: str | None = None
    title: str | None = None

    def to_meta_hint(self) -> dict[str, Any]:
        hint: dict[str, Any] = {"offline": self.offline}
        if self.title:
            hint["title"] = self.title
        if self.captured_at:
            hint["started_at"] = self.captured_at.isoformat()
        if self.duration_seconds is not None:
            hint["duration_seconds"] = self.duration_seconds
        if self.capture_id:
            hint["source_interaction_id"] = self.capture_id
        if self.language_hint:
            hint["language_hint"] = self.language_hint
        if self.seller_name or self.seller_email:
            hint["seller_hint"] = {"name": self.seller_name, "email": self.seller_email}
        if any((self.client_name, self.client_org, self.client_email, self.client_phone)):
            hint["client_hint"] = {
                "name": self.client_name, "org": self.client_org, "email": self.client_email, "phone": self.client_phone,
            }
            hint["participants"] = [{"name": self.client_name or self.client_org, "email": self.client_email}]
        return hint


def _flatten(raw: Mapping[str, Any]) -> dict[str, Any]:
    flat = dict(raw)
    for prefix in ("seller", "client"):
        nested = raw.get(prefix)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                flat.setdefault(f"{prefix}_{key}", value)
            flat.pop(prefix, None)
    return flat


def parse_mobile_sidecar(raw: bytes | str | Mapping[str, Any] | None) -> MobileSidecar:
    """Sidecar JSON (bytes, text or an already decoded dict) -> MobileSidecar; None / empty gives all defaults."""
    if raw is None or raw == b"" or raw == "":
        return MobileSidecar()
    data = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(data, Mapping):
        raise ValueError("mobile sidecar must be a JSON object")
    flat = _flatten(data)
    if flat.get("captured_at") is not None:
        try:
            datetime.fromisoformat(str(flat["captured_at"]).replace("Z", "+00:00"))
        except ValueError:
            logger.warning("mobile sidecar captured_at is not ISO-8601; ignoring it")
            flat["captured_at"] = None
    return MobileSidecar.model_validate(flat)


def render_mobile_sidecar(call: ScriptedCall) -> dict[str, Any]:
    """The sidecar a phone would send for this call (seller, client hints, capture time, offline flag)."""
    customer = call.customers[0] if call.customers else None
    return {
        "capture_id": f"mobile-{call.fixture_id}",
        "seller": {"name": call.seller.display_name, "email": call.seller.email},
        "client": {
            "name": customer.display_name if customer else None,
            "org": call.customer_org or (customer.org if customer else None),
            "email": customer.email if customer else None,
            "phone": customer.phone if customer else None,
        },
        "captured_at": parse_started_at(call).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": float(call.duration_minutes * 60),
        "offline": True,
        "language_hint": call.language,
        "title": call.title,
    }

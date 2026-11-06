"""Source-system adapters: parse (and render, for fixtures) Teams / Zoom / Genesys / mobile payloads.

Every channel converges on the canonical model: `detect_and_parse` turns an uploaded file (or a payload fetched from a
platform) into a `ParsedSource` - either transcript segments or audio to transcribe centrally - and `render_for_channel`
does the reverse for synthetic fixtures, producing the channel's *native* payload so demos exercise the real parsers.

  Teams    WebVTT with <v Speaker> voice tags           (teams.py: Graph notification + download helpers)
  Zoom     WebVTT "Speaker: text" or JSON `timeline`    (zoom.py: webhook signature, recording files, download)
  Genesys  Speech & Text Analytics transcript JSON      (genesys.py: notification parsing, OAuth client)
  Mobile   audio (+ optional JSON sidecar)              (mobile.py: audio sniffing, sidecar)
  Any      VoiceCRM canonical conversation JSON

`ParsedSource.meta_hint` keys (all optional): title, started_at (ISO-8601), duration_seconds, participants
[{name, email}], seller_hint {name, email}, client_hint {name, org, email, phone}, direction, market, business_line,
call_type, customer_org, language_hint, offline, communication_id.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path, PurePath
from typing import Any, Literal

from pydantic import Field

from ...domain.base import VModel
from ...domain.conversation import CanonicalConversation, Segment
from ...domain.enums import Channel, TranscriptOrigin
from ...domain.fixtures import ScriptedCall
from . import genesys, mobile, teams, zoom
from .common import interaction_id, scripted_to_canonical, unique_in_order
from .language import detect_language
from .mobile import AudioInfo, MobileSidecar, audio_from_hints, parse_mobile_sidecar, sniff_audio
from .recordings import find_recording

logger = logging.getLogger(__name__)

_TEXT_CONTENT_TYPES = {"text/vtt", "application/json", "text/json"}


class ParsedSource(VModel):
    kind: Literal["transcript", "audio"]
    channel: Channel
    segments: list[Segment] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)          # raw speaker labels, in order of first appearance
    language: str | None = None
    origin: TranscriptOrigin | None = None
    source_interaction_id: str | None = None
    meta_hint: dict[str, Any] = Field(default_factory=dict)    # title, started_at, duration_seconds, participants, ...
    audio_filename: str | None = None
    audio_content_type: str | None = None
    audio_bytes: bytes | None = None


class RenderedPayload(VModel):
    filename: str
    content_type: str
    data: bytes
    source_interaction_id: str
    channel: Channel
    audio_seconds: float | None = None         # set when the payload is a real recording of the call (not a placeholder)


# ---- parsing -----------------------------------------------------------------------------------------------------------
def _transcript(
    channel: Channel, segments: list[Segment], origin: TranscriptOrigin, *, language: str | None = None,
    source_interaction_id: str | None = None, meta_hint: dict[str, Any] | None = None,
) -> ParsedSource:
    if not segments:
        raise ValueError("the transcript contains no speech")
    hint = dict(meta_hint or {})
    hint.setdefault("duration_seconds", max(s.end_ms for s in segments) / 1000)
    return ParsedSource(
        kind="transcript", channel=channel, segments=segments, speakers=unique_in_order(s.speaker_id for s in segments),
        language=language or detect_language(s.text for s in segments), origin=origin,
        source_interaction_id=source_interaction_id, meta_hint=hint,
    )


def _audio(channel: Channel, data: bytes, filename: str | None, info: AudioInfo, hint: dict[str, Any] | None = None,
           source_interaction_id: str | None = None) -> ParsedSource:
    name = PurePath(filename).name if filename else f"audio{info.extension}"
    return ParsedSource(
        kind="audio", channel=channel, audio_filename=name, audio_content_type=info.content_type, audio_bytes=data,
        meta_hint=hint or {}, source_interaction_id=source_interaction_id,
    )


def _canonical_hint(conv: CanonicalConversation) -> dict[str, Any]:
    meta = conv.meta
    hint: dict[str, Any] = {
        "started_at": meta.started_at.isoformat(), "direction": meta.direction.value, "market": meta.market,
        "business_line": meta.business_line,
        "seller_hint": {"name": meta.seller.display_name, "email": meta.seller.email},
        "participants": [{"name": p.display_name, "email": p.email} for p in meta.participants],
    }
    for key, value in (("title", meta.title), ("duration_seconds", meta.duration_seconds), ("call_type", meta.call_type),
                       ("customer_org", meta.customer_org), ("language_hint", meta.language_hint)):
        if value is not None:
            hint[key] = value
    return hint


def _parse_json(text: str, channel: Channel) -> ParsedSource:
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ValueError("the file is not valid JSON") from exc
    if isinstance(obj, dict) and "meta" in obj and "segments" in obj:
        conv = CanonicalConversation.model_validate(obj)
        return _transcript(channel, list(conv.segments), conv.transcript_origin, language=conv.language,
                           source_interaction_id=conv.meta.source_interaction_id, meta_hint=_canonical_hint(conv))
    if isinstance(obj, dict) and isinstance(obj.get("transcripts"), list):
        meta = genesys.genesys_meta(obj)
        return _transcript(channel, genesys.parse_genesys_transcript(obj), TranscriptOrigin.genesys_platform,
                           source_interaction_id=meta.pop("source_interaction_id", None), meta_hint=meta)
    if isinstance(obj, dict) and isinstance(obj.get("timeline"), list):
        meta = zoom.zoom_json_meta(obj)
        return _transcript(channel, zoom.parse_zoom_transcript_json(obj), TranscriptOrigin.zoom_platform,
                           source_interaction_id=meta.pop("source_interaction_id", None), meta_hint=meta)
    raise ValueError(
        "unrecognised JSON transcript: expected a VoiceCRM conversation, a Genesys transcript (transcripts[].phrases[]) "
        "or a Zoom transcript (timeline[])"
    )


def _parse_vtt(text: str, channel: Channel) -> ParsedSource:
    if teams.looks_like_teams_vtt(text):
        return _transcript(channel, teams.parse_teams_vtt(text), TranscriptOrigin.teams_platform,
                           source_interaction_id=teams.transcript_guid(text))
    return _transcript(channel, zoom.parse_zoom_vtt(text), TranscriptOrigin.zoom_platform)


def detect_and_parse(filename: str | None, content_type: str | None, data: bytes, *, channel: Channel) -> ParsedSource:
    """Classify an uploaded or fetched file by its content and parse it.

    audio (wav/mp3/m4a/ogg/webm)        -> kind="audio"
    .vtt, Teams (`<v Speaker>`) or Zoom -> kind="transcript", origin teams_platform / zoom_platform
    .json: Genesys / Zoom / canonical   -> kind="transcript"
    Raises ValueError for empty, unsupported or unparseable input (map it to HTTP 400/422).
    """
    if not data:
        raise ValueError("the file is empty")
    info = sniff_audio(data, filename)
    if info is not None:
        return _audio(channel, data, filename, info)

    declared = (content_type or "").split(";")[0].strip().lower()
    suffix = PurePath(filename or "").suffix.lower()
    text = data.decode("utf-8-sig", errors="replace")
    head = text.lstrip("\ufeff \t\r\n")
    if head.upper().startswith("WEBVTT") or suffix == ".vtt" or declared == "text/vtt":
        return _parse_vtt(text, channel)
    if suffix == ".json" or declared in _TEXT_CONTENT_TYPES or head.startswith(("{", "[")):
        return _parse_json(text, channel)

    hinted = audio_from_hints(filename, content_type)
    if hinted is not None:
        return _audio(channel, data, filename, hinted)
    raise ValueError(
        f"unsupported file {filename or '(unnamed)'!r}: "
        "expected audio (.wav .mp3 .m4a .ogg .webm), a .vtt or a .json transcript"
    )


def parse_mobile_upload(
    audio: bytes, filename: str | None, content_type: str | None, sidecar: bytes | str | dict[str, Any] | None = None,
    *, channel: Channel = Channel.mobile,
) -> ParsedSource:
    """A mobile recording plus its optional JSON sidecar -> an audio ParsedSource carrying the capture hints."""
    if not audio:
        raise ValueError("the recording is empty")
    info = sniff_audio(audio, filename) or audio_from_hints(filename, content_type)
    if info is None:
        raise ValueError("the recording is not a supported audio format (.wav .mp3 .m4a .ogg .webm)")
    meta = parse_mobile_sidecar(sidecar)
    hint = meta.to_meta_hint()
    return _audio(channel, audio, filename, info, hint=hint, source_interaction_id=hint.pop("source_interaction_id", None))


# ---- rendering ---------------------------------------------------------------------------------------------------------
def render_for_channel(
    call: ScriptedCall, channel: Channel | None = None, *, as_audio: bool = False,
    zoom_format: Literal["vtt", "json"] = "vtt", recordings_dir: Path | str | None = None,
) -> RenderedPayload:
    """The scripted call as the channel's native payload.

    teams -> WebVTT with voice tags; zoom -> WebVTT (or JSON with zoom_format="json"); genesys -> transcript JSON;
    mobile / upload -> the VoiceCRM canonical conversation JSON. With as_audio=True: the real recording of the call when
    `recordings_dir` holds one (see recordings.py), otherwise a tiny valid silent WAV named `fixture-<fixture_id>.wav`
    (about the call length, at most 30 s) that the mock transcriber maps back to the call.
    """
    target = channel or call.channel
    source_id = interaction_id(call, target)
    if as_audio:
        recording = find_recording(call.fixture_id, recordings_dir)
        if recording is not None:
            return RenderedPayload(
                filename=recording.path.name, content_type=recording.content_type, data=recording.path.read_bytes(),
                source_interaction_id=source_id, channel=target, audio_seconds=recording.seconds)
        return RenderedPayload(
            filename=f"fixture-{call.fixture_id}.wav", content_type="audio/wav",
            data=mobile.silent_wav(mobile.audio_seconds(call)), source_interaction_id=source_id, channel=target,
        )
    if target == Channel.teams:
        name, ctype, text = f"teams-{call.fixture_id}.vtt", "text/vtt", teams.render_teams_vtt(call)
    elif target == Channel.zoom and zoom_format == "json":
        name, ctype = f"zoom-{call.fixture_id}.json", "application/json"
        text = json.dumps(zoom.render_zoom_transcript_json(call), ensure_ascii=False, indent=2)
    elif target == Channel.zoom:
        name, ctype, text = f"zoom-{call.fixture_id}.vtt", "text/vtt", zoom.render_zoom_vtt(call)
    elif target == Channel.genesys:
        name, ctype = f"genesys-{call.fixture_id}.json", "application/json"
        text = json.dumps(genesys.render_genesys_transcript(call), ensure_ascii=False, indent=2)
    else:
        name, ctype = f"{target.value}-{call.fixture_id}.json", "application/json"
        text = scripted_to_canonical(call, target).model_dump_json(indent=2)
    return RenderedPayload(filename=name, content_type=ctype, data=text.encode("utf-8"), source_interaction_id=source_id,
                           channel=target)


__all__ = [
    "AudioInfo", "MobileSidecar", "ParsedSource", "RenderedPayload", "detect_and_parse", "genesys", "mobile",
    "parse_mobile_sidecar", "parse_mobile_upload", "render_for_channel", "sniff_audio", "teams", "zoom",
]

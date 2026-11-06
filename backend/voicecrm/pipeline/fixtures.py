"""Turns a ScriptedCall into the IngestInput a real source adapter would produce (used by simulate, eval and the CLI).

It renders the call in the channel's native payload format and parses it back with the real source adapter, so a
simulated Zoom call exercises the same parser a live Zoom webhook would.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain.conversation import ConsentInfo, ConversationMeta
from ..domain.enums import Channel, Direction, SpeakerRole
from ..domain.fixtures import ScriptedCall
from .models import IngestInput


def _parse_started(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ingest_from_scripted(
    call: ScriptedCall,
    *,
    channel: Channel | None = None,
    market: str | None = None,
    business_line: str | None = None,
    template_id: str | None = None,
    via: str = "transcript",
    asr_noise: bool = False,
    engine: str | None = None,
    nonce: str | None = None,
    recordings_dir: Path | str | None = None,
) -> IngestInput:
    from ..adapters.sources import detect_and_parse, render_for_channel

    ch = channel or call.channel
    payload = render_for_channel(call, ch, as_audio=(via == "audio"), recordings_dir=recordings_dir)
    parsed = detect_and_parse(payload.filename, payload.content_type, payload.data, channel=ch)
    started = _parse_started(call.started_at)
    seconds = payload.audio_seconds or call.duration_minutes * 60.0            # a real recording has its own length
    seller = call.seller.model_copy(update={"role": SpeakerRole.seller})
    people = [
        *(p.model_copy(update={"role": SpeakerRole.customer}) for p in call.customers),
        *(p.model_copy(update={"role": SpeakerRole.internal}) for p in call.internal),
    ]
    interaction = payload.source_interaction_id + (f"-{nonce}" if nonce else "")
    meta = ConversationMeta(
        source=ch,
        source_interaction_id=interaction,
        direction=call.direction if ch == call.channel else (Direction.inbound if ch == Channel.genesys else call.direction),
        title=call.title,
        started_at=started,
        ended_at=started + timedelta(seconds=seconds),
        duration_seconds=seconds,
        seller=seller,
        participants=people,
        customer_org=call.customer_org,
        market=market or call.market,
        business_line=business_line or call.business_line,
        call_type=call.call_type,
        language_hint=call.language,
        consent=ConsentInfo(status="recorded_with_consent", basis="synthetic demo fixture"),
    )
    return IngestInput(
        meta=meta,
        kind=parsed.kind,
        segments=parsed.segments,
        speakers=parsed.speakers,
        transcript_origin=parsed.origin,
        language=parsed.language,
        audio_bytes=parsed.audio_bytes,
        audio_filename=parsed.audio_filename,
        audio_content_type=parsed.audio_content_type,
        fixture_id=call.fixture_id,
        asr_noise=asr_noise,
        requested_template_id=template_id,
        engine=engine,
    )

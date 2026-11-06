"""Helpers shared by the source-system adapters: deterministic ids, timings, ScriptedCall -> canonical conversion.

The renderers turn a ScriptedCall (a synthetic demo call) into each platform's native transcript format, so fixtures
travel through the same parsers a real Teams / Zoom / Genesys payload would. Everything here is deterministic: the
same call always renders to the same bytes (stable ids, synthesised but fixed timings).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from ...domain.conversation import CanonicalConversation, ConversationMeta, Participant, Segment
from ...domain.enums import Channel, TranscriptOrigin
from ...domain.fixtures import ScriptedCall, ScriptedTurn

UNKNOWN_SPEAKER = "Unknown"

_DEFAULT_IDS = {"seller": "seller", "customer": "cust1", "internal": "int1"}
_ROLE_LABELS = {"seller": "Seller", "customer": "Customer", "internal": "Colleague"}

LEAD_IN_MS = 1500
GAP_MS = 400
MS_PER_WORD = 380
MIN_TURN_MS = 1200


def unique_in_order(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def parse_started_at(call: ScriptedCall) -> datetime:
    started = datetime.fromisoformat(call.started_at.replace("Z", "+00:00"))
    return started if started.tzinfo else started.replace(tzinfo=timezone.utc)


def default_speaker_id(turn: ScriptedTurn) -> str:
    return turn.speaker_id or _DEFAULT_IDS[turn.speaker]


def participants_by_id(call: ScriptedCall) -> dict[str, Participant]:
    return {p.id: p for p in [call.seller, *call.customers, *call.internal]}


def speaker_label(call: ScriptedCall, turn: ScriptedTurn, lookup: dict[str, Participant] | None = None) -> str:
    """The name a platform would show for the speaker of this turn."""
    people = lookup if lookup is not None else participants_by_id(call)
    person = people.get(default_speaker_id(turn))
    return person.display_name if person else _ROLE_LABELS[turn.speaker]


def turn_timings(call: ScriptedCall) -> list[tuple[int, int]]:
    """Fixed, plausible (start_ms, end_ms) per turn: about 160 words per minute with short gaps."""
    timings: list[tuple[int, int]] = []
    cursor = LEAD_IN_MS
    for turn in call.turns:
        duration = max(MIN_TURN_MS, len(turn.text.split()) * MS_PER_WORD)
        timings.append((cursor, cursor + duration))
        cursor += duration + GAP_MS
    return timings


def interaction_id(call: ScriptedCall, channel: Channel) -> str:
    """Stable id of the call in the (simulated) source system; the same fixture always gets the same id."""
    if channel in (Channel.teams, Channel.genesys):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicecrm:{channel.value}:{call.fixture_id}"))
    return f"{channel.value}-{call.fixture_id}"


def communication_id(call: ScriptedCall) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicecrm:genesys-communication:{call.fixture_id}"))


def scripted_to_canonical(
    call: ScriptedCall, channel: Channel | None = None, origin: TranscriptOrigin = TranscriptOrigin.fixture
) -> CanonicalConversation:
    channel = channel or call.channel
    started = parse_started_at(call)
    duration = call.duration_minutes * 60
    segments = [
        Segment(id=index, speaker_id=default_speaker_id(turn), start_ms=start, end_ms=end, text=turn.text,
                locale=turn.locale)
        for index, (turn, (start, end)) in enumerate(zip(call.turns, turn_timings(call), strict=True))
    ]
    meta = ConversationMeta(
        conversation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"voicecrm:conversation:{call.fixture_id}").hex,
        source=channel, source_interaction_id=interaction_id(call, channel), direction=call.direction,
        title=call.title, started_at=started, ended_at=started + timedelta(seconds=duration),
        duration_seconds=float(duration), seller=call.seller, participants=[*call.customers, *call.internal],
        customer_org=call.customer_org, market=call.market, business_line=call.business_line,
        call_type=call.call_type, language_hint=call.language,
    )
    return CanonicalConversation(meta=meta, segments=segments, language=call.language, transcript_origin=origin)


def reindex(segments: Sequence[Segment]) -> list[Segment]:
    """Sequential 0-based ids in the order given."""
    return [s.model_copy(update={"id": i}) for i, s in enumerate(segments)]


def participants_hint(participants: Iterable[Participant]) -> list[dict[str, str | None]]:
    return [{"name": p.display_name, "email": p.email} for p in participants]

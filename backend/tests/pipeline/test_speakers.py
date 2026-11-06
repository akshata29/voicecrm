"""Speaker mapping for diarized audio: every scripted call, opened the way a real recording would be labelled."""
from __future__ import annotations

import pytest

from voicecrm.domain.conversation import ConversationMeta, Participant, Segment
from voicecrm.domain.enums import Direction, SpeakerRole
from voicecrm.pipeline.fixtures import ingest_from_scripted
from voicecrm.pipeline.speakers import map_speakers

from datetime import datetime, timezone


def meta(direction: Direction = Direction.outbound, customers: tuple[str, ...] = ("Dana Whitfield",)) -> ConversationMeta:
    return ConversationMeta(
        source="teams", source_interaction_id="x", direction=direction, started_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
        seller=Participant(id="seller", display_name="Sam Rivera", role=SpeakerRole.seller),
        participants=[Participant(id=f"cust{i + 1}", display_name=name, role=SpeakerRole.customer) for i, name in enumerate(customers)])


def segs(*turns: tuple[str, str]) -> list[Segment]:
    return [Segment(id=i, speaker_id=label, start_ms=i * 1000, end_ms=i * 1000 + 900, text=text) for i, (label, text) in enumerate(turns)]


def roles(result: list[Segment]) -> list[str]:
    return [s.speaker_id for s in result]


def test_named_speakers_are_matched_to_participants():
    out, people = map_speakers(segs(("Sam Rivera", "Hi."), ("Dana Whitfield", "Hello.")), meta())
    assert roles(out) == ["seller", "cust1"] and {p.id for p in people} == {"seller", "cust1"}


def test_the_first_speaker_is_the_seller_when_nobody_names_themselves():
    out, _ = map_speakers(segs(("S1", "Thanks for joining."), ("S2", "Happy to."), ("S1", "Let's start.")), meta())
    assert roles(out) == ["seller", "cust1", "seller"]


def test_an_inbound_agent_who_greets_first_is_still_the_seller():
    out, _ = map_speakers(segs(("S1", "Thank you for calling client services, how can I help?"),
                               ("S2", "Hi, I have a question about a distribution.")), meta(Direction.inbound))
    assert roles(out) == ["seller", "cust1"]


def test_who_greets_whom_by_name_beats_who_speaks_first():
    # the customer happens to speak first, but the greetings show who is who
    out, _ = map_speakers(segs(("S1", "Hello? Sam, is that you?"), ("S2", "Hi Dana, it's Sam. Thanks for calling back."),
                               ("S1", "Yes, thanks Sam.")), meta())
    assert roles(out) == ["cust1", "seller", "cust1"]


def test_a_self_introduction_identifies_the_speaker():
    out, _ = map_speakers(segs(("S1", "Hi, this is Dana Whitfield from Harborview."), ("S2", "Morning, Sam here.")), meta())
    assert roles(out) == ["cust1", "seller"]


def test_greeting_someone_by_name_means_you_are_not_them():
    out, _ = map_speakers(segs(("S1", "Good morning, Sam."), ("S2", "Good morning, Dana, thanks for making the time.")), meta())
    assert roles(out) == ["cust1", "seller"]


def test_a_single_unlabelled_speaker_follows_the_direction():
    assert roles(map_speakers(segs(("S1", "A dictated note after the meeting.")), meta())[0]) == ["seller"]
    assert roles(map_speakers(segs(("S1", "Voicemail from a client.")), meta(Direction.inbound))[0]) == ["cust1"]


def test_several_customers_are_assigned_in_order_once_the_seller_is_known():
    out, _ = map_speakers(segs(("S1", "Morning everyone."), ("S2", "Morning, Sam."), ("S3", "Hi Sam, good to see you.")),
                          meta(customers=("Dana Whitfield", "Alex Kim")))
    assert roles(out) == ["seller", "cust1", "cust2"]


def test_a_named_customer_leaves_the_unlabelled_speaker_to_the_seller_even_inbound():
    out, _ = map_speakers(segs(("Dana Whitfield", "Hello?"), ("S1", "Thank you for calling.")), meta(Direction.inbound))
    assert roles(out) == ["cust1", "seller"]


@pytest.fixture(scope="module")
def scripted_calls():
    from voicecrm.config import REPO_ROOT
    from voicecrm.data_loader import load_scripted_calls

    return load_scripted_calls(REPO_ROOT / "data")


def test_every_scripted_call_gets_its_roles_right_when_labelled_like_diarization(scripted_calls):
    """Label turns S1, S2, ... in order of first appearance (what a diarizer outputs) and check the roles come back."""
    checked = 0
    for call in scripted_calls:
        if any(t.speaker == "internal" for t in call.turns):
            continue                                         # a seller-side colleague has no unambiguous role from the text alone
        label_of: dict[str, str] = {}
        segments = []
        for i, turn in enumerate(call.turns):
            key = turn.speaker_id or turn.speaker
            label = label_of.setdefault(key, f"S{len(label_of) + 1}")
            segments.append(Segment(id=i, speaker_id=label, start_ms=i * 1000, end_ms=i * 1000 + 900, text=turn.text))
        mapped, _ = map_speakers(segments, ingest_from_scripted(call).meta)
        for turn, seg in zip(call.turns, mapped, strict=True):
            got = "seller" if seg.speaker_id == call.seller.id else "customer"
            assert got == ("seller" if turn.speaker == "seller" else "customer"), (call.fixture_id, turn.text[:60])
        checked += 1
    assert checked >= 14

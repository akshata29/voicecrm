"""Notes policy: PII placeholders, sensitive omission, rapport facts and small-talk labelling."""
from __future__ import annotations

import pytest

from voicecrm.domain.enums import SpeakerRole
from voicecrm.domain.policy import NotesPolicy
from voicecrm.engines.notes_policy import RulesNotesPolicyEngine
from voicecrm.ports.pii import PiiSpan

from .helpers import make_conversation

CARD = "4111 1111 1111 1111"
EMAIL = "priya@northwind.example"


def span(text: str, needle: str, category: str) -> PiiSpan:
    start = text.index(needle)
    return PiiSpan(start=start, end=start + len(needle), category=category, text=needle)


def apply(turns, *, notes=None, spans=None):
    conv = make_conversation(turns)
    return conv, RulesNotesPolicyEngine().apply(conv, notes=notes or NotesPolicy(), pii_spans=spans or {})


def test_pii_spans_become_placeholders_in_segments_and_in_the_redacted_conversation():
    card, mail = f"My card number is {CARD} and it expires soon.", f"You can email me at {EMAIL} please."
    conv, res = apply([("c", card), ("c", mail)], spans={0: [span(card, CARD, "payment_card")],
                                                         1: [span(mail, EMAIL, "personal_contact")]})
    assert res.segments[0].text == "My card number is [PAYMENT_CARD] and it expires soon."
    assert res.segments[1].text == "You can email me at [PERSONAL_CONTACT] please."
    assert [s.text for s in res.redacted_conversation.segments] == [s.text for s in res.segments]
    assert CARD in conv.segments[0].text                                      # the input conversation is never mutated
    assert {a.category for a in res.actions if a.kind == "pii_redacted"} == {"payment_card", "personal_contact"}


def test_overlapping_pii_spans_collapse_into_one_placeholder_without_leaking_digits():
    text = f"Account {CARD} thanks"
    start = text.index(CARD)
    spans = [PiiSpan(start=start, end=start + 12, category="payment_card", text=""),
             PiiSpan(start=start + 6, end=start + 19, category="bank_account", text="")]
    _, res = apply([("c", text)], spans={0: spans})
    assert res.segments[0].text == "Account [PAYMENT_CARD] thanks" and "4111" not in res.segments[0].text


def test_categories_outside_the_policy_are_left_untouched():
    text = f"You can email me at {EMAIL} please."
    _, res = apply([("c", text)], notes=NotesPolicy(omit_categories=["payment_card"]),
                   spans={0: [span(text, EMAIL, "personal_contact")]})
    assert res.segments[0].text == text and not res.actions


@pytest.mark.parametrize("text", [
    "My wife was diagnosed with cancer last spring, so timing matters.",
    "I need to check her chemotherapy schedule before we meet.",
    "My father has been in the hospital, so I may be out next week.",
    "Mi esposa tiene cáncer, así que el momento es importante.",
])
def test_medical_and_family_health_disclosures_are_omitted_everywhere(text):
    _, res = apply([("c", text)])
    segment, redacted = res.segments[0], res.redacted_conversation.segments[0]
    assert segment.label == "sensitive" and segment.text == ""
    assert redacted.text.startswith("[omitted: ") and "cancer" not in redacted.text.lower()
    assert [a.kind for a in res.actions] == ["omitted_sensitive"]


@pytest.mark.parametrize("text", [
    "What is the tax treatment of the conversion?",
    "We manage the hospital pension plan for four thousand employees.",
    "The doctor of finance on the committee agrees with the allocation.",
])
def test_business_sentences_that_only_sound_medical_are_kept(text):
    _, res = apply([("c", text)])
    assert res.segments[0].label == "business" and res.segments[0].text == text and not res.actions


@pytest.mark.parametrize("text, category, keyword", [
    ("I'm a huge Seahawks fan, by the way.", "sports", "seattle seahawks"),
    ("I love golf on weekends.", "hobby", "golf"),
    ("I grew up in Spokane, Washington.", "local", "spokane"),
    ("I prefer email over phone calls.", "contact_preference", "email"),
])
def test_relationship_facts_are_kept_with_evidence_when_the_policy_allows(text, category, keyword):
    _, res = apply([("c", text)])
    (kept,) = res.relationship_facts
    assert kept.category == category and keyword in kept.text.lower()
    assert kept.text != text                                                   # a distilled fact, not the transcript line
    assert kept.evidence[0].segment_ids == [0] and kept.evidence[0].quote in text
    assert res.segments[0].label == "relationship"
    assert [a.kind for a in res.actions] == ["kept_relationship"]


def test_relationship_facts_are_not_captured_without_keep_relationship_context():
    _, res = apply([("c", "I'm a huge Seahawks fan, by the way.")], notes=NotesPolicy(keep_relationship_context=False))
    assert res.relationship_facts == [] and res.segments[0].label == "business" and not res.actions


def test_rapport_inside_a_business_turn_keeps_the_fact_and_the_business_label():
    _, res = apply([("c", "I'm a huge Seahawks fan, and we are interested in the equity fund allocation.")])
    assert [f.text for f in res.relationship_facts] == ["Supports the Seattle Seahawks"]
    assert res.segments[0].label == "business"


def test_greetings_and_weather_are_small_talk_only_when_the_policy_drops_it():
    turns = [("s", "Good morning! How are you?"), ("c", "Nice weather today, isn't it?"), ("c", "Buenos días, ¿cómo está?"),
             ("c", "We want to review the fee schedule for the bond fund.")]
    _, dropped = apply(turns)
    assert [s.label for s in dropped.segments] == ["small_talk", "small_talk", "small_talk", "business"]
    assert [a.kind for a in dropped.actions] == ["dropped_small_talk"] and dropped.actions[0].segment_ids == [0, 1, 2]
    _, kept = apply(turns, notes=NotesPolicy(drop_small_talk=False))
    assert {s.label for s in kept.segments} == {"business"}


def test_policy_actions_never_echo_transcript_text():
    card = f"My card is {CARD}."
    turns = [("c", card), ("c", "My wife has cancer, so timing matters."), ("c", "I'm a huge Seahawks fan."),
             ("s", "Good morning, how are you?")]
    _, res = apply(turns, spans={0: [span(card, CARD, "payment_card")]})
    assert {a.kind for a in res.actions} == {"pii_redacted", "omitted_sensitive", "kept_relationship", "dropped_small_talk"}
    forbidden = ("4111", "1111", "cancer", "wife", "Seahawks", "morning")
    assert not [a for a in res.actions if any(word.lower() in a.detail.lower() for word in forbidden)]


def test_speaker_roles_ids_and_order_are_carried_over():
    _, res = apply([("s", "Thanks for the update on the fund."), ("c", "We will review the proposal.")])
    assert [(s.id, s.speaker_role) for s in res.segments] == [(0, SpeakerRole.seller), (1, SpeakerRole.customer)]
    assert [s.id for s in res.redacted_conversation.segments] == [0, 1]

"""Task engine: relative-date resolution (documented conventions) and seller-commitment extraction, English + Spanish."""
from __future__ import annotations

import re
from datetime import date

import pytest

from voicecrm.domain.enums import OwnerRole
from voicecrm.engines.tasks import RulesTaskEngine, resolve_due

from .helpers import make_meta, seg, segments

ANCHOR = date(2026, 11, 10)                                                  # a Tuesday


@pytest.fixture(scope="module")
def engine() -> RulesTaskEngine:
    return RulesTaskEngine()


def extract(engine, *turns, language="en", meta=None):
    return engine.extract(segments(*turns), meta or make_meta(), language=language)


@pytest.mark.parametrize("phrase, expected", [
    ("tomorrow", date(2026, 11, 11)), ("by Friday", date(2026, 11, 13)),
    ("Tuesday", date(2026, 11, 17)),                                         # a bare weekday is strictly after the call date
    ("next Tuesday", date(2026, 11, 17)), ("next Friday", date(2026, 11, 20)),
    ("end of the week", date(2026, 11, 13)), ("end of next week", date(2026, 11, 20)),
    ("in two days", date(2026, 11, 12)), ("in 2 weeks", date(2026, 11, 24)), ("today", ANCHOR),
    ("el viernes", date(2026, 11, 13)), ("mañana", date(2026, 11, 11)), ("a finales de la próxima semana", date(2026, 11, 20)),
    ("el próximo lunes", date(2026, 11, 16)), ("en dos semanas", date(2026, 11, 24)),
])
def test_resolve_due_follows_the_documented_conventions(phrase, expected):
    due = resolve_due(phrase, ANCHOR)
    assert due.day == expected and due.basis == "stated" and due.text


@pytest.mark.parametrize("phrase", ["next week", "soon", "sometime", ""])
def test_vague_or_missing_dates_resolve_to_no_date(phrase):
    due = resolve_due(phrase, ANCHOR)
    assert due.day is None and due.basis == "none"


def test_fuzzy_counts_are_inferred_and_other_anchor_days_keep_the_convention():
    assert resolve_due("in a few days", ANCHOR).basis == "inferred"
    monday = date(2026, 11, 16)
    assert resolve_due("Friday", monday).day == date(2026, 11, 20)           # this week's Friday ...
    assert resolve_due("next Friday", monday).day == date(2026, 11, 27)      # ... but "next" means the following Mon-Sun week
    assert resolve_due("end of the week", date(2026, 11, 14)).day == date(2026, 11, 20)     # Saturday -> the coming Friday


@pytest.mark.parametrize("phrase, expected", [("by EOD Friday", date(2026, 11, 13)), ("by COB tomorrow", date(2026, 11, 11)),
                                              ("by end of day tomorrow", date(2026, 11, 11))])
def test_end_of_day_followed_by_a_day_uses_that_day(phrase, expected):
    assert resolve_due(phrase, ANCHOR).day == expected
    assert resolve_due("by EOD", ANCHOR).day == ANCHOR                            # on its own, end of day still means today


@pytest.mark.parametrize("phrase", ["Le envío el informe mañana por la tarde", "Se lo mando mañana por la mañana"])
def test_spanish_tomorrow_afternoon_or_morning_is_tomorrow(phrase):
    assert resolve_due(phrase, ANCHOR).day == date(2026, 11, 11)


def test_spanish_time_of_day_alone_is_not_a_date_and_tomorrow_afternoon_dates_the_task(engine):
    assert all(resolve_due(p, ANCHOR).day is None for p in ("Te lo envío por la mañana", "Te lo envío por la tarde"))
    (task,) = extract(engine, ("s", "Le envío el informe mañana por la tarde."), language="es")
    assert task.due_date == date(2026, 11, 11) and task.title.isascii()


def test_seller_commitment_becomes_a_dated_task_with_evidence(engine):
    (task,) = extract(engine, ("s", "I'll send you the fee comparison by Friday."))
    assert "fee comparison" in task.title.lower()
    assert (task.due_date, task.due_basis) == (date(2026, 11, 13), "stated") and "friday" in task.due_text.lower()
    assert (task.owner_role, task.owner_ref, task.task_id) == (OwnerRole.seller, "seller", "t1")
    assert task.evidence[0].segment_ids == [0] and task.evidence[0].quote == "I'll send you the fee comparison by Friday."
    assert 0.5 <= task.confidence <= 0.97


def test_customer_promises_are_context_not_tasks(engine):
    assert extract(engine, ("c", "I'll take it to my committee.")) == []
    assert extract(engine, ("c", "I'll send you our questionnaire by Friday.")) == []


@pytest.mark.parametrize("text", [
    "Should I send you the fee comparison by Friday?",                         # question
    "I won't send the proposal by Friday.", "I can't send the proposal by Friday.",         # negation
    "Maybe I can send the proposal by Friday.", "I might send the proposal by Friday.",     # hedge
    "If you approve, I'll send the subscription documents tomorrow.",                       # conditional offer
    "I can send you the deck.",                                                             # offer nobody accepted
])
def test_questions_negations_hedges_and_unaccepted_offers_are_not_tasks(engine, text):
    assert extract(engine, ("s", text)) == []


def test_an_accepted_offer_is_a_lower_confidence_task(engine):
    (task,) = extract(engine, ("s", "I can send you the deck."), ("c", "Yes, please do."))
    assert "deck" in task.title.lower() and task.confidence < 0.8 and task.due_date is None


def test_let_me_get_the_fact_sheet_over_to_you_tomorrow(engine):
    (task,) = extract(engine, ("s", "Let me get the updated fact sheet over to you tomorrow."))
    assert "fact sheet" in task.title.lower() and task.due_date == date(2026, 11, 11)


def test_compound_commitment_yields_two_tasks_sharing_the_date(engine):
    tasks = extract(engine, ("s", "I'll send the deck and schedule a call with the portfolio manager next Tuesday."))
    assert [t.task_id for t in tasks] == ["t1", "t2"]
    assert "deck" in tasks[0].title.lower() and "portfolio manager" in tasks[1].title.lower()
    assert {t.due_date for t in tasks} == {date(2026, 11, 17)}


def test_restating_the_same_deliverable_does_not_duplicate_the_task(engine):
    assert len(extract(engine, ("s", "I'll send the proposal and send it to you by Friday."))) == 1
    assert len(extract(engine, ("s", "I'll send the fee comparison by Friday."), ("s", "I'll send the fee comparison by Friday."))) == 1


def test_spanish_commitment_gets_an_english_title_and_date(engine):
    (task,) = extract(engine, ("s", "Le envío la comparación de comisiones el viernes."), language="es")
    assert {"fee", "comparison"} <= set(task.title.lower().split()) and task.title.isascii()
    assert task.due_date == date(2026, 11, 13) and task.owner_role == OwnerRole.seller


def test_colleague_commitments_are_internal_and_hidden_segments_are_ignored(engine):
    segs = [seg(0, "We'll update the model portfolio by Thursday.", "i"),
            seg(1, "I'll send the deck tomorrow.", "s", label="small_talk"), seg(2, "", "s", label="sensitive")]
    (task,) = engine.extract(segs, make_meta(), language="en")
    assert task.owner_role == OwnerRole.internal and task.owner_ref == "colleague" and task.due_date == date(2026, 11, 12)


def test_sentence_initial_team_commitment_is_a_task(engine):
    (task,) = extract(engine, ("s", "Our research team will send the commentary on Thursday."))
    assert "commentary" in task.title.lower() and task.due_date == date(2026, 11, 12)


@pytest.mark.parametrize("text", [
    "Dana will send the paperwork by Friday.", "I think Dana will send the paperwork by Friday.",
    "Your team will send the signed forms by Friday.", "The client team will send the signed forms by Friday.",
])
def test_a_customer_side_subject_is_the_customers_promise_not_a_seller_task(engine, text):
    assert extract(engine, ("s", text), meta=make_meta(customer="Dana Whitfield")) == []


def test_a_named_colleague_who_is_not_on_the_call_still_gets_a_task(engine):
    (task,) = extract(engine, ("s", "Maria will send the paperwork by Friday."), meta=make_meta(customer="Dana Whitfield"))
    assert "paperwork" in task.title.lower() and task.due_date == date(2026, 11, 13) and task.owner_role == OwnerRole.seller


@pytest.mark.parametrize("text, due", [
    ("I'll send the proposal by EOD Friday.", date(2026, 11, 13)),
    ("I'll send the proposal by COB tomorrow.", date(2026, 11, 11)),
    ("I'll send the proposal by end of day Friday.", date(2026, 11, 13)),
    ("I'll send the proposal by close of business next Tuesday.", date(2026, 11, 17)),
])
def test_end_of_day_deadlines_use_the_named_day_and_leave_no_due_words_in_the_title(engine, text, due):
    (task,) = extract(engine, ("s", text))
    assert task.due_date == due and "proposal" in task.title.lower()
    assert not re.search(r"\b(?:by|eod|cob|end of day|close of business|friday|tuesday|tomorrow)\b", task.title, re.IGNORECASE)

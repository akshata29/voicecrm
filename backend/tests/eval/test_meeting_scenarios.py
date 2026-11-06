"""The Teams meeting recordings (data/meeting): each script, played into one microphone, through the real rules pipeline.

A hermetic stand-in for the live rehearsal (tools/live_check.py --meeting): the transcript is what Teams produces for audio
played into a single device (one speaker label), the engine is the rules baseline, and every call is scored against the gold
labels in its script. The two scenarios that show human review are asserted as such: the fund called by its former name (the
reviewer teaches the alias once, and the next call resolves it) and the share class that is never named (the reviewer picks
one per call; nothing is learned, because the phrase never named a class).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from voicecrm.config import REPO_ROOT, Settings
from voicecrm.container import Container, build_container
from voicecrm.domain.pipeline import ApproveRequest
from voicecrm.eval.metrics import score_case
from voicecrm.pipeline.fixtures import ingest_from_scripted

STEMS = sorted(path.stem for path in (REPO_ROOT / "data" / "meeting").glob("*.json"))
AMBIGUOUS, LEGACY = "ambiguous-fund-call", "legacy-name-call"
NEEDS_A_PICK = {AMBIGUOUS, LEGACY}


@pytest.fixture(scope="module")
def tool() -> Iterator[Any]:
    spec = importlib.util.spec_from_file_location("gen_meeting_audio", REPO_ROOT / "tools" / "gen_meeting_audio.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                        # dataclasses resolves string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Container]:
    settings = Settings(
        _env_file=None, state_dir=tmp_path_factory.mktemp("meeting-state"), demo_mode=True, transcription_mode="mock",
        llm_mode="rules", search_mode="local", pii_mode="local", crm_mode="local", vault_mode="local")
    built = build_container(settings)
    try:
        yield built
    finally:
        built.close()


def play(c: Container, call: Any) -> Any:
    return asyncio.run(c.service.run_to_completion(ingest_from_scripted(call, via="transcript", nonce=uuid4().hex[:6])))


def approve_with(c: Container, rec: Any, product_id: str) -> Any:
    """The reviewer picks a product for the call's one mention and approves (what the Review screen sends)."""
    request = ApproveRequest(product_choices={rec.proposal.result.products[0].mention_id: product_id})
    return asyncio.run(c.service.approve(rec.meta.conversation_id, request))


def families(c: Container) -> dict[str, str]:
    return {p.product_id: p.family_id for p in c.deps.catalogue.products}


@pytest.fixture(scope="module")
def calls(tool: Any) -> dict[str, Any]:
    return {path.stem: tool.to_scripted_call(script, path.stem) for path, script in tool.load_scripts()}


@pytest.fixture(scope="module")
def played(container: Container, calls: dict[str, Any]) -> dict[str, tuple[Any, Any, Any]]:
    family_of = families(container)
    out = {}
    for stem, call in calls.items():
        rec = play(container, call)
        out[stem] = (call, rec, score_case(call, rec, family_of=family_of))
    return out


def test_every_script_was_played(played: dict[str, Any]) -> None:
    assert sorted(played) == STEMS and len(STEMS) >= 2


@pytest.mark.parametrize("stem", STEMS)
def test_the_call_reaches_review_with_the_harborview_contact(played: dict[str, Any], stem: str) -> None:
    _, rec, _ = played[stem]
    assert rec.status.value == "needs_review" and rec.proposal is not None
    crm = rec.proposal.crm_match
    assert (crm.account.id, crm.contact.name, crm.method) == ("acc-001", "Dana Whitfield", "email")


@pytest.mark.parametrize("stem", STEMS)
def test_topics_and_tasks_match_the_gold_labels_with_nothing_leaked(played: dict[str, Any], stem: str) -> None:
    _, _, case = played[stem]
    assert (case.topic_fp, case.topic_fn) == (0, 0)
    assert (case.task_fp, case.task_fn) == (0, 0)
    assert case.product_false_tags == 0 and case.leaks == 0


@pytest.mark.parametrize("stem", [s for s in STEMS if s != LEGACY])
def test_the_right_product_is_among_the_candidates(played: dict[str, Any], stem: str) -> None:
    _, _, case = played[stem]
    assert case.product_top3_hits == case.product_gold


@pytest.mark.parametrize("stem", [s for s in STEMS if s not in NEEDS_A_PICK])
def test_a_clear_product_is_tagged_first_time_and_needs_no_pick(played: dict[str, Any], stem: str) -> None:
    _, rec, case = played[stem]
    assert case.product_top1_correct == case.details["gold_nonambiguous"]
    assert "product_needs_pick" not in rec.proposal.review.reasons


@pytest.mark.parametrize("stem", sorted(NEEDS_A_PICK))
def test_the_two_review_scenarios_ask_a_person_instead_of_guessing(played: dict[str, Any], stem: str) -> None:
    _, rec, _ = played[stem]
    assert "product_needs_pick" in rec.proposal.review.reasons and not rec.proposal.review.auto_approve_eligible
    assert rec.proposal.result.products and all(m.chosen_product_id is None for m in rec.proposal.result.products)


def test_the_ambiguous_call_offers_the_equity_income_share_classes(played: dict[str, Any]) -> None:
    _, rec, _ = played[AMBIGUOUS]
    offered = {c.product_id for m in rec.proposal.result.products for c in m.candidates}
    assert "SYN-000009" in offered and offered <= {f"SYN-00000{n}" for n in range(6, 10)}


def test_a_reviewer_teaches_the_former_fund_name_once_and_the_next_call_resolves_it(container: Container,
                                                                                    calls: dict[str, Any]) -> None:
    call = calls[LEGACY]
    iso = container.isolated()
    try:
        first = play(iso, call)
        assert first.proposal.result.products[0].reasons == ["no_candidate"]
        approve_with(iso, first, "SYN-000002")
        second = play(iso, call)
        case = score_case(call, second, family_of=families(iso))
    finally:
        iso.close()
    assert [(m.chosen_product_id, m.reasons) for m in second.proposal.result.products] == [("SYN-000002", ["learned_alias"])]
    assert "product_needs_pick" not in second.proposal.review.reasons and second.proposal.review.auto_approve_eligible
    assert (case.product_top1_correct, case.product_false_tags) == (1, 0)


def test_a_share_class_that_was_never_said_is_picked_per_call_and_not_learned(container: Container,
                                                                              calls: dict[str, Any]) -> None:
    call = calls[AMBIGUOUS]
    iso = container.isolated()
    try:
        first = play(iso, call)
        done = approve_with(iso, first, "SYN-000009")
        again = play(iso, call)
        learned = iso.deps.store.list_aliases()
    finally:
        iso.close()
    assert done.proposal.result.products[0].chosen_product_id == "SYN-000009"
    assert learned == [] and "product_needs_pick" in again.proposal.review.reasons


def test_the_privacy_call_redacts_the_card_and_the_cell_and_omits_the_medical_aside(played: dict[str, Any]) -> None:
    _, rec, case = played["privacy-call"]
    actions = {(a.kind, a.category) for a in rec.proposal.policy_actions}
    assert {("pii_redacted", "payment_card"), ("pii_redacted", "personal_contact"), ("omitted_sensitive", "medical")} <= actions
    note = rec.proposal.note_markdown.lower()
    assert case.leaks == 0 and not any(secret in note for secret in ("4111", "206 555", "surgery", "surgeon", "knee"))


def test_a_call_with_no_commitments_gets_no_tasks(played: dict[str, Any]) -> None:
    _, rec, _ = played["no-actions-call"]
    assert rec.proposal.result.tasks == []


def test_the_competitor_is_a_topic_and_not_a_product(played: dict[str, Any]) -> None:
    _, rec, _ = played["competitor-call"]
    assert "competitor_comparison" in {t.topic_id for t in rec.proposal.result.topics}
    assert {m.chosen_product_id for m in rec.proposal.result.products} == {"SYN-000004"}


def test_the_etf_and_the_mutual_fund_are_two_products(played: dict[str, Any]) -> None:
    _, rec, _ = played["etf-vs-fund-call"]
    assert {m.chosen_product_id for m in rec.proposal.result.products} == {"SYN-000054", "SYN-000005"}


def test_the_spanish_call_is_read_as_spanish_and_comes_out_with_english_tasks(played: dict[str, Any]) -> None:
    _, rec, case = played["spanish-call"]
    assert rec.language == "es-US" and case.task_tp == 2
    assert all(t.title.startswith("Send") for t in rec.proposal.result.tasks)


def test_weekday_deadlines_resolve_against_the_meeting_date(played: dict[str, Any]) -> None:
    _, rec, _ = played["class-c-call"]
    assert sorted(str(t.due_date) for t in rec.proposal.result.tasks) == ["2026-11-09", "2026-11-11"]

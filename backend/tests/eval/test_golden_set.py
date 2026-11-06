"""Golden-set quality gates: every scripted call through the real pipeline, scored against its gold labels.

The calls tagged `alias_learning` use a legacy fund name that shares no vocabulary with the catalogue, so by design
the baseline engine cannot resolve them until a reviewer teaches the alias ("improves over time" demo). The baseline
gates therefore score the calls the engine is expected to resolve; the learning calls have their own tests (never
mis-tagged and always routed to a human at baseline, fully resolved once the aliases are learned).
"""
from __future__ import annotations

import asyncio
import operator
from collections.abc import Callable, Iterator

import pytest

from voicecrm.config import Settings
from voicecrm.container import Container, build_container
from voicecrm.domain.evaluation import EvalCaseResult, EvalMetrics, EvalRun
from voicecrm.eval.harness import run_eval
from voicecrm.eval.metrics import aggregate

LEARNING_TAG = "alias_learning"
Gate = tuple[str, Callable[[float, float], bool], float]
BASELINE_GATES: list[Gate] = [
    ("product_top1", operator.ge, 0.95), ("product_top3", operator.eq, 1.0), ("product_false_tag_rate", operator.eq, 0.0),
    ("privacy_leaks", operator.eq, 0), ("topic_recall", operator.ge, 0.9), ("task_recall", operator.ge, 0.9),
    ("relationship_kept_rate", operator.eq, 1.0),
]
NOISE_GATES: list[Gate] = [("product_top1", operator.ge, 0.9), ("privacy_leaks", operator.eq, 0)]
STABLE_METRICS = ("product_top1", "product_top3", "product_false_tag_rate", "share_class_confusion_rate",
                  "topic_precision", "topic_recall", "task_precision", "task_recall", "privacy_leaks",
                  "relationship_kept_rate", "auto_approvable_rate")
STABLE_CASE_FIELDS = {"fixture_id", "product_top1_correct", "product_top3_hits", "task_tp", "topic_tp", "leaks"}


def is_learning_case(case: EvalCaseResult) -> bool:
    return LEARNING_TAG in case.tags


def gated(run: EvalRun) -> EvalMetrics:
    """Metrics over the calls the baseline engine is expected to resolve."""
    return aggregate([case for case in run.cases if not is_learning_case(case)])


def _gate_id(gate: Gate) -> str:
    return f"{gate[0]}{'>=' if gate[1] is operator.ge else '=='}{gate[2]}"


def _misses(run: EvalRun) -> list[tuple[str, int, int, int]]:
    """(fixture, top1 hits, non-ambiguous gold, top3 hits) for every gated call that missed a product."""
    return [(c.fixture_id, c.product_top1_correct, c.details["gold_nonambiguous"], c.product_top3_hits)
            for c in run.cases if not is_learning_case(c)
            and (c.product_top1_correct < c.details["gold_nonambiguous"] or c.product_top3_hits < c.product_gold)]


@pytest.fixture(scope="module")
def container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Container]:
    """The 'real' container (nothing isolated): evaluation must leave it untouched."""
    settings = Settings(
        _env_file=None, state_dir=tmp_path_factory.mktemp("eval-state"), demo_mode=True, transcription_mode="mock",
        llm_mode="rules", search_mode="local", pii_mode="local", crm_mode="local", vault_mode="local")
    built = build_container(settings)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture(scope="module")
def baseline(container: Container) -> EvalRun:
    return asyncio.run(run_eval(container))


@pytest.fixture(scope="module")
def baseline_again(container: Container, baseline: EvalRun) -> EvalRun:
    return asyncio.run(run_eval(container))


@pytest.fixture(scope="module")
def noisy(container: Container, baseline: EvalRun) -> EvalRun:
    return asyncio.run(run_eval(container, with_asr_noise=True))


@pytest.fixture(scope="module")
def learned(container: Container, baseline: EvalRun) -> EvalRun:
    return asyncio.run(run_eval(container, apply_learning=True))


# ---- the golden set itself ---------------------------------------------------------------------------------------------
def test_golden_set_covers_every_scripted_call(container: Container, baseline: EvalRun) -> None:
    assert len(container.scripted_calls) >= 12
    assert baseline.metrics.cases == len(baseline.cases) == len(container.scripted_calls)
    assert {c.fixture_id for c in baseline.cases} == set(container.scripted_calls)
    assert (baseline.engine, baseline.with_asr_noise, baseline.label, baseline.learned_aliases) == (
        "rules", False, "baseline", 0)


def test_every_scripted_call_has_a_gold_template_that_exists(container: Container, baseline: EvalRun) -> None:
    for fixture_id, call in container.scripted_calls.items():
        assert call.gold.template_id in container.deps.templates, fixture_id
    for case in baseline.cases:
        assert case.details["template"] == container.scripted_calls[case.fixture_id].gold.template_id, case.fixture_id


def test_every_call_reaches_review_with_the_expected_crm_account(baseline: EvalRun) -> None:
    for case in baseline.cases:
        assert case.details["status"] == "needs_review", (case.fixture_id, case.details.get("error"))
        assert case.details["crm_ok"] is True, (case.fixture_id, case.details["crm_account"], case.details["crm_expected"])
        assert case.latency_ms is not None and case.cost_usd is not None


# ---- baseline quality gates ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("gate", BASELINE_GATES, ids=_gate_id)
def test_baseline_meets_the_quality_gate(baseline: EvalRun, gate: Gate) -> None:
    metric, compare, threshold = gate
    value = getattr(gated(baseline), metric)
    assert compare(value, threshold), f"{metric}={value} (required {_gate_id(gate)}); misses: {_misses(baseline)}"


def test_baseline_never_confuses_share_classes_of_the_right_fund(baseline: EvalRun) -> None:
    assert baseline.metrics.share_class_confusion_rate == 0
    assert baseline.metrics.product_false_tag_rate == 0


def test_a_product_the_engine_misses_is_always_routed_to_a_human(baseline: EvalRun) -> None:
    for case in baseline.cases:
        if case.product_top1_correct < case.details["gold_nonambiguous"]:
            assert case.needs_review and "product_needs_pick" in case.details["review_reasons"], case.fixture_id


def test_baseline_scores_are_deterministic(baseline: EvalRun, baseline_again: EvalRun) -> None:
    for metric in STABLE_METRICS:
        assert getattr(baseline.metrics, metric) == getattr(baseline_again.metrics, metric), metric
    first = [c.model_dump(include=STABLE_CASE_FIELDS) for c in baseline.cases]
    assert first == [c.model_dump(include=STABLE_CASE_FIELDS) for c in baseline_again.cases]


# ---- ASR noise ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("gate", NOISE_GATES, ids=_gate_id)
def test_asr_noise_keeps_the_quality_gate(noisy: EvalRun, gate: Gate) -> None:
    metric, compare, threshold = gate
    value = getattr(gated(noisy), metric)
    assert compare(value, threshold), f"{metric}={value} under ASR noise (required {_gate_id(gate)})"


def test_asr_noise_run_is_labelled_and_never_leaks_overall(noisy: EvalRun, baseline: EvalRun) -> None:
    assert noisy.with_asr_noise is True and noisy.label == "baseline + ASR noise"
    assert noisy.metrics.cases == baseline.metrics.cases
    assert noisy.metrics.privacy_leaks == 0
    assert noisy.metrics.product_false_tag_rate == 0


# ---- the improvement loop (legacy-name calls) -------------------------------------------------------------------------------
def test_legacy_name_calls_are_never_mis_tagged_at_baseline(baseline: EvalRun) -> None:
    learning = [c for c in baseline.cases if is_learning_case(c)]
    assert learning, "the golden set should keep at least one alias-learning call"
    for case in learning:
        assert case.product_false_tags == 0, case.fixture_id
        resolved = case.product_top1_correct == case.details["gold_nonambiguous"]
        assert resolved or "product_needs_pick" in case.details["review_reasons"], case.fixture_id


def test_learning_from_reviewer_corrections_resolves_the_legacy_name_calls(baseline: EvalRun, learned: EvalRun) -> None:
    learning = [c for c in learned.cases if is_learning_case(c)]
    assert learned.label == "learned"
    assert learned.learned_aliases >= len(learning) > 0
    for case in learning:
        assert case.product_top1_correct == case.details["gold_nonambiguous"], case.fixture_id
        assert case.product_top3_hits == case.product_gold, case.fixture_id
    assert learned.metrics.product_top1 >= baseline.metrics.product_top1
    assert (learned.metrics.product_top1, learned.metrics.product_top3) == (1.0, 1.0)
    assert (learned.metrics.product_false_tag_rate, learned.metrics.privacy_leaks) == (0, 0)
    assert learned.metrics.topic_recall >= 0.9 and learned.metrics.task_recall >= 0.9


# ---- isolation ---------------------------------------------------------------------------------------------------------------
def test_run_eval_is_isolated_from_the_real_container(container: Container, baseline: EvalRun, noisy: EvalRun,
                                                      learned: EvalRun) -> None:
    store = container.deps.store
    assert store.list_conversations(limit=1000) == []
    assert store.list_aliases() == []
    assert store.list_eval_runs() == []
    for entity in ("phonecall", "task", "insight"):
        assert asyncio.run(container.deps.crm.list_records(entity)) == [], entity
    assert asyncio.run(container.deps.vault.list_items()) == []
    assert {c.fixture_id for c in baseline.cases} == set(container.scripted_calls)

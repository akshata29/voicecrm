"""PipelineStats: queue depth, run boundaries, percentiles, rates - and the pipeline's wiring into it."""
from __future__ import annotations

from voicecrm.adapters.llm.errors import LlmOutputError
from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.config import Settings
from voicecrm.container import _load_bundle
from voicecrm.domain.enums import ConversationStatus
from voicecrm.pipeline.fixtures import ingest_from_scripted
from voicecrm.pipeline.models import IngestInput
from voicecrm.pipeline.stats import PipelineStats

from .test_llm_pipeline import _model, _service

CALLS = list(_load_bundle(str(Settings(_env_file=None).data_dir)).scripted_calls.values())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_stats(max_concurrency: int = 4) -> tuple[PipelineStats, Clock]:
    stats, clock = PipelineStats(max_concurrency=max_concurrency), Clock()
    stats._clock = clock
    return stats, clock


def test_an_idle_snapshot_is_empty():
    stats, _ = make_stats()
    snap = stats.snapshot()
    assert (snap.active, snap.submitted, snap.queued, snap.in_flight, snap.completed, snap.failed) == (False, 0, 0, 0, 0, 0)
    assert snap.calls_per_minute is None and snap.eta_seconds is None and snap.stages_ms == {}
    assert snap.max_concurrency == 4


def test_queue_depth_and_in_flight_follow_the_lifecycle():
    stats, clock = make_stats()
    for _ in range(3):
        stats.submitted()
    snap = stats.snapshot()
    assert (snap.active, snap.queued, snap.in_flight) == (True, 3, 0)
    stats.started(0.4)
    snap = stats.snapshot()
    assert (snap.queued, snap.in_flight, snap.completed) == (2, 1, 0)
    clock.advance(2)
    stats.finished(ok=True, total_seconds=2.4)
    stats.started(1.0)
    stats.started(1.2)
    stats.finished(ok=False, total_seconds=3.0, error="PipelineError: ingest carries neither a transcript nor audio")
    snap = stats.snapshot()
    assert (snap.completed, snap.failed, snap.queued, snap.in_flight, snap.active) == (1, 1, 0, 1, True)
    assert [(e.error, e.count) for e in snap.errors] == [("PipelineError: ingest carries neither a transcript nor audio", 1)]
    stats.finished(ok=True, total_seconds=4.0)
    snap = stats.snapshot()
    assert (snap.completed, snap.failed, snap.in_flight, snap.active) == (2, 1, 0, False)
    assert snap.end_to_end_seconds.count == 2 and snap.end_to_end_seconds.errors == 1
    assert snap.queue_wait_seconds.max == 1.2


def test_a_new_run_starts_when_the_pipeline_was_idle_but_lifetime_totals_remain():
    stats, clock = make_stats()
    for _ in range(2):
        stats.submitted()
        stats.started(0.0)
        stats.stage("extract", 900)
        stats.finished(ok=True, total_seconds=1.0, cost_usd=0.01)
    clock.advance(600)
    stats.submitted()
    snap = stats.snapshot()
    assert (snap.submitted, snap.completed, snap.queued) == (1, 0, 1)
    assert snap.stages_ms == {} and snap.cost_usd == 0 and snap.elapsed_seconds == 0
    assert snap.lifetime_completed == 2


def test_a_short_pause_does_not_end_the_batch_but_a_long_one_does():
    stats, clock = make_stats()
    for _ in range(3):                                              # calls trickle in and each finishes before the next
        stats.submitted()
        stats.started(0.0)
        clock.advance(2.0)
        stats.finished(ok=True, total_seconds=2.0)
        clock.advance(2.0)
    snap = stats.snapshot()
    assert (snap.submitted, snap.completed, snap.active) == (3, 3, False) and snap.elapsed_seconds == 10.0
    clock.advance(30)
    stats.submitted()
    assert (stats.snapshot().submitted, stats.snapshot().completed) == (1, 0)
    assert stats.snapshot().lifetime_completed == 3


def test_stage_percentiles_skip_nothing_recorded_and_count_errors_separately():
    stats, _ = make_stats()
    for ms in (100, 200, 300, 400, 1000):
        stats.stage("extract", ms)
    stats.stage("extract", 5000, ok=False)
    stats.stage("policy", 50)
    extract = stats.snapshot().stages_ms["extract"]
    assert (extract.count, extract.p50, extract.p95, extract.max, extract.mean, extract.errors) == (5, 300, 1000, 1000, 400, 1)
    assert stats.snapshot().stages_ms["policy"].count == 1


def test_throughput_and_eta_track_the_pace_of_the_run():
    stats, clock = make_stats()
    for _ in range(100):
        stats.submitted()
    for _ in range(30):
        stats.started(0.0)
        clock.advance(1.0)
        stats.finished(ok=True, total_seconds=1.0)
    snap = stats.snapshot()
    assert snap.active and snap.completed == 30 and snap.elapsed_seconds == 30
    assert snap.calls_per_minute == 60.0 and snap.recent_calls_per_minute == 60.0
    assert snap.eta_seconds == 70                                  # 70 calls left at one per second


def test_the_rate_is_not_reported_for_a_sub_second_burst():
    stats, clock = make_stats()
    stats.submitted()
    stats.started(0.0)
    clock.advance(0.2)
    stats.finished(ok=True, total_seconds=0.2)
    snap = stats.snapshot()
    assert snap.completed == 1 and snap.calls_per_minute is None and snap.recent_calls_per_minute is None


def test_tokens_cost_engines_and_fallbacks_accumulate():
    stats, _ = make_stats()
    stats.submitted()
    stats.submitted()
    stats.started(0.0)
    stats.started(0.0)
    stats.extracted("hybrid", 3000, 900, fallback=False)
    stats.extracted("rules", 0, 0, fallback=True)
    stats.finished(ok=True, total_seconds=1.0, cost_usd=0.02)
    stats.finished(ok=False, total_seconds=1.0, error="boom", cost_usd=0.005)
    snap = stats.snapshot()
    assert (snap.tokens_in, snap.tokens_out, snap.fallbacks, snap.engines) == (3000, 900, 1, {"hybrid": 1, "rules": 1})
    assert snap.cost_usd == 0.025


async def test_the_pipeline_feeds_the_stats(tmp_path):
    h = _service(tmp_path, FakeLlm(_model, model="fake-gpt"))
    calls = CALLS[:4]
    for index, call in enumerate(calls):
        await h.service.submit(ingest_from_scripted(call, nonce=f"s{index}", via="audio" if index == 0 else "transcript"))
    await h.service.drain()
    snap = h.service.stats.snapshot()
    assert (snap.submitted, snap.completed, snap.failed, snap.queued, snap.in_flight) == (len(calls), len(calls), 0, 0, 0)
    assert not snap.active and snap.end_to_end_seconds.count == len(calls)
    assert {"ingest", "normalize", "policy", "extract", "resolve", "validate"} <= set(snap.stages_ms)
    assert snap.stages_ms["transcribe"].count == 1                  # only the audio call transcribes; platform transcripts skip it
    assert snap.engines == {"hybrid": len(calls)} and snap.fallbacks == 0
    assert snap.tokens_in > 0 and snap.tokens_out > 0 and snap.cost_usd > 0


async def test_duplicates_are_not_counted_and_failures_are(tmp_path):
    h = _service(tmp_path, FakeLlm(_model))
    ing = ingest_from_scripted(h.call, nonce="dup")
    await h.service.submit(ing)
    await h.service.submit(ing)
    empty = ingest_from_scripted(h.call, nonce="empty")
    empty = IngestInput(meta=empty.meta.model_copy(update={"source_interaction_id": "empty-1"}), kind="transcript",
                        segments=[], speakers=[])
    await h.service.submit(empty)
    await h.service.drain()
    snap = h.service.stats.snapshot()
    assert (snap.submitted, snap.completed, snap.failed) == (2, 1, 1)
    assert snap.errors and "neither a transcript nor audio" in snap.errors[0].error
    assert h.store.get_conversation(empty.meta.conversation_id).status == ConversationStatus.failed


async def test_a_model_failure_that_falls_back_counts_as_a_fallback_not_a_failure(tmp_path):
    h = _service(tmp_path, FakeLlm([LlmOutputError("boom")]))
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, nonce="fb"))
    snap = h.service.stats.snapshot()
    assert rec.status == ConversationStatus.needs_review
    assert (snap.completed, snap.failed, snap.fallbacks, snap.engines) == (1, 0, 1, {"rules": 1})


def test_begin_batch_starts_clean_only_when_nothing_is_unfinished():
    stats, _ = make_stats()
    stats.submitted()
    stats.started(0.0)
    stats.stage("extract", 800)
    stats.finished(ok=True, total_seconds=1.0, cost_usd=0.02)
    stats.begin_batch()
    snap = stats.snapshot()
    assert (snap.submitted, snap.completed, snap.cost_usd, snap.stages_ms) == (0, 0, 0, {})
    assert snap.lifetime_completed == 1                                  # lifetime totals are never reset
    stats.submitted()
    stats.begin_batch()                                                  # a call is still unfinished: its batch is kept
    assert stats.snapshot().submitted == 1

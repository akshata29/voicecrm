"""LoadRunner: the plan, the estimate, the safeguards, a real run on the local adapters, stop, auto-approve and cleanup."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from voicecrm.config import ResolvedMode, Settings
from voicecrm.container import Container, build_container
from voicecrm.domain.enums import Channel
from voicecrm.pipeline import load as load_module
from voicecrm.pipeline.load import LoadRequest, LoadStatus
from voicecrm.pipeline.service import PipelineError
from voicecrm.pipeline.stats import RunSnapshot, SeriesSummary

LIVE_NAMES = {"pii": "azure_language", "transcription": "azure_speech", "llm": "hybrid", "crm": "dataverse"}


@pytest.fixture()
def container(settings: Settings) -> Iterator[Container]:
    c = build_container(settings)
    try:
        yield c
    finally:
        c.close()


class BilledPii:
    """Stands in for Azure AI Language: bills packed records, finds nothing, remembers the concurrency it was given."""

    name = "azure_language"

    def __init__(self, concurrency: int = 4) -> None:
        self.concurrency = concurrency
        self.seen: list[int] = []

    def text_records(self, texts: list[str]) -> int:
        return max(1, len(texts) // 5)

    def set_concurrency(self, limit: int) -> int:
        self.concurrency = limit
        return limit

    async def detect_many(self, texts: list[str], *, language: str) -> list[list]:
        self.seen.append(self.concurrency)
        return [[] for _ in texts]


def go_live(c: Container, *services: str) -> None:
    for name in services:
        c.modes[name] = ResolvedMode(service=name, requested=LIVE_NAMES[name], effective=LIVE_NAMES[name], live=True)


async def settled(c: Container, timeout: float = 90.0) -> LoadStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = c.load.status()
        if status.state in ("finished", "stopped", "failed"):
            return status
        await asyncio.sleep(0.05)
    await c.load.stop()
    pytest.fail(f"the load test was still {c.load.status().state} after {timeout} s")


async def sending(c: Container, calls: int = 1, timeout: float = 30.0) -> None:
    """Waits until the run has handed at least `calls` calls to the pipeline."""
    deadline = time.monotonic() + timeout
    while c.load.status().sent < calls and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert c.load.status().sent >= calls


# ---- estimate and safeguards ----------------------------------------------------------------------------------------
def test_an_estimate_on_local_adapters_is_free_and_sized_by_the_plan(container):
    est = container.load.estimate(LoadRequest(count=40))
    assert (est.calls, est.via, est.engine, est.pool) == (40, "transcript", "rules", len(container.scripted_calls))
    assert (est.billed, est.cost_usd, est.tokens_in, est.pii_records, est.speech_hours) == ([], 0, 0, 0, 0)
    assert est.parallel == 16 and est.seconds > 0 and est.calls_per_minute > 0


def test_an_estimate_follows_the_chosen_calls_channels_and_pace(container):
    one = container.load.estimate(LoadRequest(count=10, fixtures=["pb-teams-equity-class-c"]))
    assert one.pool == 1
    zoom = container.load.estimate(LoadRequest(count=10, channels=[Channel.zoom]))
    assert zoom.pool == sum(1 for c in container.scripted_calls.values() if c.channel == Channel.zoom) > 0
    slow = container.load.estimate(LoadRequest(count=60, rate=1.0))
    assert slow.calls_per_minute == 60 and slow.seconds >= 60                      # arrivals, not the pipeline, set the pace
    assert container.load.estimate(LoadRequest(count=5, parallel=3)).parallel == 3


@pytest.mark.parametrize("body,fragment", [
    ({"count": 2001}, "limited to 2,000"),
    ({"count": 5, "fixtures": ["no-such-call"]}, "unknown calls"),
    ({"count": 5, "engine": "hybrid"}, "no model is configured"),
])
def test_requests_the_run_could_not_honour_are_refused_with_a_reason(container, body, fragment):
    with pytest.raises(PipelineError) as refused:
        container.load.estimate(LoadRequest(**body))
    assert refused.value.status == 422 and fragment in str(refused.value)


def test_a_load_test_never_auto_approves_into_a_live_crm(container):
    go_live(container, "crm")
    with pytest.raises(PipelineError) as refused:
        container.load.estimate(LoadRequest(count=5, auto_approve=True))
    assert refused.value.status == 422 and "live CRM" in str(refused.value)
    assert container.load.estimate(LoadRequest(count=5)).calls == 5                # without auto-approve nothing is written


async def test_billed_services_are_listed_priced_and_need_confirmation(container, monkeypatch):
    monkeypatch.setattr(container.deps, "pii", BilledPii())
    monkeypatch.setattr(container.settings, "foundry_deployment", "chat41mini")
    container.deps.extractors["hybrid"] = container.deps.extractors["rules"]
    container.deps.default_engine = "hybrid"
    go_live(container, "pii", "llm")
    est = container.load.estimate(LoadRequest(count=100))
    assert est.billed == ["Azure AI Language", "Foundry model (chat41mini)"]
    assert (est.tokens_in, est.tokens_out) == (310_000, 55_000) and est.pii_records > 0
    assert {line.service for line in est.lines} == {"language_pii", "llm:chat41mini"}
    assert est.cost_usd == pytest.approx(sum(line.est_cost for line in est.lines), abs=1e-4) and est.cost_usd > 0
    assert "typical for the hybrid engine" in est.basis
    with pytest.raises(PipelineError) as refused:
        await container.load.start(LoadRequest(count=5))
    assert refused.value.status == 409 and "confirmation required" in str(refused.value) and "Azure AI Language" in str(refused.value)
    assert container.load.status().state == "idle" and container.deps.store.count_batch() == 0


def test_a_live_speech_run_sends_only_calls_that_have_a_recording(container, tmp_path, monkeypatch):
    monkeypatch.setattr(container.settings, "data_dir", tmp_path / "no-data")      # no audio\live folder here
    go_live(container, "transcription")
    with pytest.raises(PipelineError) as refused:
        container.load.estimate(LoadRequest(count=3, via="audio"))
    assert refused.value.status == 422 and "make_audio" in str(refused.value)
    assert container.load.estimate(LoadRequest(count=3, via="transcript")).billed == []   # a transcript run needs no Speech


# ---- running --------------------------------------------------------------------------------------------------------
async def test_a_run_sends_every_call_tags_it_and_reports_what_it_took(container):
    started = await container.load.start(LoadRequest(count=8, parallel=4))
    assert started.state == "preparing" and started.planned == 8 and started.parallel == 4
    final = await settled(container)
    report = final.report
    assert final.state == "finished" and report is not None
    assert (report.calls, report.finished, report.failed, report.planned) == (8, 8, 0, 8)
    assert sum(report.by_status.values()) == 8 and report.by_status.get("needs_review", 0) + report.by_status.get("written", 0) == 8
    assert report.parallel == 4 and report.pipeline.completed == 8 and report.pipeline.max_concurrency == 4
    assert report.latency_p50_seconds is not None and report.calls_per_minute and report.projections[0].calls == 5000
    assert "4 in parallel" in report.projection_basis and report.projections[0].seconds > 0
    assert report.modes["transcription"] == "mock" and not report.stopped_early
    assert (final.sent, final.finished, final.in_flight, final.queued, final.stored_calls) == (8, 8, 0, 0, 8)
    assert container.deps.store.count_batch() == 8
    assert {r.batch_id for r in container.deps.store.list_conversations(limit=50)} == {f"load-{final.run_id}"}
    assert container.service.stats.max_concurrency == container.deps.max_concurrency        # the run's setting is undone


async def test_clear_removes_the_calls_their_history_and_what_they_wrote_to_the_crm(container):
    await container.load.start(LoadRequest(count=18, auto_approve=True, parallel=8, seed=3))
    final = await settled(container)
    assert final.report.approved > 0 and final.report.by_status.get("written") == final.report.approved
    crm = container.deps.crm
    assert len(await crm.list_records("phonecall", 500)) == final.report.approved
    ids = container.deps.store.batch_conversation_ids()
    assert len(ids) == 18
    assert await container.load.clear() == 18
    assert container.deps.store.count_batch() == 0 and container.deps.store.list_conversations(limit=50) == []
    assert all(container.deps.store.list_events(cid) == [] and container.deps.store.list_usage(cid) == [] for cid in ids)
    assert await crm.list_records("phonecall", 500) == [] and await crm.list_records("insight", 500) == []
    assert [await container.deps.vault.list_items(cid) for cid in ids] == [[]] * 18
    assert await container.load.clear() == 0
    assert container.load.status().stored_calls == 0


async def test_auto_approve_only_writes_the_calls_that_need_no_human(container):
    await container.load.start(LoadRequest(count=18, auto_approve=False, seed=3))
    without = (await settled(container)).report
    assert without.approved == 0 and without.by_status.get("written", 0) == 0
    assert without.straight_through > 0                                          # clean calls are counted, just not approved
    await container.load.start(LoadRequest(count=18, auto_approve=True, seed=3))
    approving = (await settled(container)).report
    assert approving.approved == approving.straight_through == without.straight_through
    assert approving.by_status["written"] == approving.approved
    assert approving.by_status.get("needs_review", 0) == 18 - approving.approved
    assert all(reason for reason in approving.review_reasons)                    # what is left carries the reason it needs a person


async def test_a_new_run_replaces_the_previous_runs_calls(container):
    await container.load.start(LoadRequest(count=6))
    first = await settled(container)
    first_ids = set(container.deps.store.batch_conversation_ids())
    await container.load.start(LoadRequest(count=4))
    second = await settled(container)
    second_ids = set(container.deps.store.batch_conversation_ids())
    assert first.run_id != second.run_id and len(first_ids) == 6 and len(second_ids) == 4 and not first_ids & second_ids


async def test_load_test_calls_do_not_disturb_calls_made_another_way(container):
    other = container.scripted_calls["pb-teams-equity-class-c"]
    from voicecrm.pipeline.fixtures import ingest_from_scripted

    mine = await container.service.run_to_completion(ingest_from_scripted(other, nonce="mine"))
    await container.load.start(LoadRequest(count=5))
    await settled(container)
    assert await container.load.clear() == 5
    assert container.deps.store.get_conversation(mine.meta.conversation_id) is not None


async def test_audio_input_goes_through_the_transcriber(container):
    await container.load.start(LoadRequest(count=3, via="audio", fixtures=["pb-teams-equity-class-c"], parallel=2))
    final = await settled(container)
    assert final.state == "finished" and final.report.failed == 0 and final.report.finished == 3
    sample = container.deps.store.list_conversations(limit=1)[0]
    stages = {e.stage.value: e.status.value for e in container.deps.store.list_events(sample.meta.conversation_id)
              if e.status.value != "started"}
    assert stages["transcribe"] == "completed"                                    # transcribed, not a platform transcript


async def test_the_run_raises_the_pii_concurrency_for_its_duration_and_restores_it(container, monkeypatch):
    pii = BilledPii(concurrency=4)
    monkeypatch.setattr(container.deps, "pii", pii)
    go_live(container, "pii")
    await container.load.start(LoadRequest(count=6, parallel=20, confirm=True))
    final = await settled(container)
    assert final.state == "finished" and final.report.failed == 0
    assert set(pii.seen) == {20} and pii.concurrency == 4


async def test_submit_failures_are_counted_not_raised(container, monkeypatch):
    request = LoadRequest(count=5, fixtures=["pb-teams-equity-class-c"])
    container.load.estimate(request)                                              # warms the estimate's own parse
    monkeypatch.setattr(load_module, "ingest_from_scripted", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    await container.load.start(request)
    final = await settled(container)
    assert final.state == "finished" and final.report.failed == 5 and final.report.by_status == {"failed": 5}
    assert final.report.errors[0].error.startswith("submit: RuntimeError") and final.report.errors[0].count == 5


# ---- control --------------------------------------------------------------------------------------------------------
async def test_stop_discards_the_unsent_calls_and_waits_for_the_ones_in_flight(container):
    await container.load.start(LoadRequest(count=200, rate=20, parallel=2))
    await sending(container, 3)
    assert container.load.status().state == "running"
    assert (await container.load.stop()).state == "stopping"
    final = await settled(container)
    assert final.state == "stopped" and final.report.stopped_early
    assert 3 <= final.sent < 200 and final.finished == final.sent and final.queued == 0 and final.in_flight == 0
    assert container.deps.store.count_batch() == final.sent


async def test_only_one_run_at_a_time_and_nothing_can_be_cleared_mid_run(container):
    await container.load.start(LoadRequest(count=100, rate=10))
    await sending(container, 2)
    for attempt in (container.load.start(LoadRequest(count=1)), container.load.clear()):
        with pytest.raises(PipelineError) as refused:
            await attempt
        assert refused.value.status == 409
    await container.load.stop()
    await settled(container)
    assert await container.load.clear() >= 2


async def test_closing_the_runner_ends_a_run_that_is_still_going(container):
    await container.load.start(LoadRequest(count=300, rate=30, parallel=2))
    await sending(container, 2)
    await container.load.aclose()
    assert container.load.status().state in ("stopped", "finished")
    await container.service.drain()


def test_projections_use_the_steady_state_rate_not_the_ramp_up_of_a_short_run(container):
    runner = container.load
    runner._parallel = 32
    snapshot = RunSnapshot(end_to_end_seconds=SeriesSummary(count=60, mean=12.0), queue_wait_seconds=SeriesSummary(count=60, mean=4.0))
    rate, basis = runner._steady_rate(snapshot, LoadRequest(count=60))
    assert rate == pytest.approx(32 / 8.0 * 60) and basis == "32 in parallel at about 8.0 s per call"
    assert runner._steady_rate(snapshot, LoadRequest(count=60, rate=2.0))[0] == pytest.approx(120)     # arrivals set the pace
    instant = RunSnapshot(end_to_end_seconds=SeriesSummary(count=9, mean=0.06), queue_wait_seconds=SeriesSummary(count=9, mean=0.0))
    assert runner._steady_rate(instant, LoadRequest(count=9))[0] == pytest.approx(20 * 60)             # one core is the limit
    assert runner._steady_rate(RunSnapshot(), LoadRequest(count=1)) is None


def test_an_idle_runner_reports_nothing_and_the_limits(container):
    status = container.load.status()
    assert (status.state, status.run_id, status.report, status.estimate, status.sent) == ("idle", None, None, None, 0)
    assert status.max_calls == 2000 and status.stored_calls == 0 and status.pipeline.submitted == 0

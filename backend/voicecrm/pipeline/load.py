"""Load test behind the Live demo page: sends N scripted calls through the real pipeline and reports what it took.

It is the in-process twin of tools/bulk_load.py. The calls are the scripted conversations cycled with unique ids, either
as the channel's native transcript or as the real recording through speech-to-text, so every call is privacy-filtered,
summarised and matched exactly as in a real run. Nothing here changes how one call is processed: the runner decides how
many are in flight, optionally auto-approves the clean ones, and measures.

Safeguards: one run at a time; a hard cap on calls; an explicit confirmation when billed Azure services are live; no
auto-approve into a live CRM; and every call a run creates carries a batch tag, so "remove load-test calls" takes them
out of the inbox, the vault, the state store and the local CRM simulator again (a new run replaces the previous one).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ..adapters.sources.recordings import find_recording
from ..domain.base import VModel
from ..domain.conversation import utcnow
from ..domain.enums import Channel, ConversationStatus, Stage
from ..domain.fixtures import ScriptedCall
from ..domain.metering import CostLine, UsageRecord
from ..domain.pipeline import ApproveRequest, ConversationRecord
from .fixtures import ingest_from_scripted
from .service import PipelineError
from .stats import ErrorCount, RunSnapshot

if TYPE_CHECKING:
    from ..container import Container

logger = logging.getLogger(__name__)

LoadState = Literal["idle", "preparing", "running", "stopping", "finished", "stopped", "failed"]
_ACTIVE = frozenset({"preparing", "running", "stopping"})

_DEFAULT_TOKENS_IN = 3100          # per call on the hybrid engine, measured on the live service (docs/live-demo.md)
_DEFAULT_TOKENS_OUT = 550
_MIN_MEASURED_CALLS = 5            # model calls needed before the last batch is trusted over the defaults
_CPU_CALLS_PER_S = 20.0            # one event loop parsing, redacting and persisting: ~1,300 calls/min on the mock adapters
_SERVICE_SECONDS = {"base": 0.05, "speech": 14.0, "language": 1.0, "model": 9.0}     # per call, per live service, under load
_PROJECT_CALLS = (5000, 20000)
_PARALLEL_LIVE, _PARALLEL_MOCK = 32, 16
_MAX_PII_CONCURRENCY = 32
_TOP_ERRORS = 5


# ---------------------------------------------------------------------------------------------------- models
class LoadRequest(VModel):
    count: int = Field(ge=1)
    via: Literal["transcript", "audio"] = "transcript"     # platform transcript, or the real recording through speech-to-text
    rate: float = Field(default=0.0, ge=0.0, le=1000.0)    # calls per second; 0 = as fast as the pipeline accepts them
    auto_approve: bool = False                             # approve (and write) the calls that need no human
    engine: Literal["rules", "llm", "hybrid"] | None = None
    fixtures: list[str] = Field(default_factory=list)      # empty = every scripted call
    channels: list[Channel] = Field(default_factory=list)  # empty = every channel
    asr_noise: bool = False
    seed: int = 7
    parallel: int | None = Field(default=None, ge=1, le=256)   # calls processed at once; None = chosen from the live services
    confirm: bool = False                                  # the caller accepts that billed services will be used


class LoadEstimate(VModel):
    calls: int
    via: str
    engine: str
    parallel: int
    pool: int                                              # distinct scripted calls the plan cycles through
    speech_hours: float = 0.0
    pii_records: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    lines: list[CostLine] = Field(default_factory=list)
    billed: list[str] = Field(default_factory=list)        # live services this run will be charged for
    seconds: float = 0.0
    calls_per_minute: float = 0.0
    basis: str = ""
    warnings: list[str] = Field(default_factory=list)
    illustrative: bool = True


class LoadProjection(VModel):
    calls: int
    seconds: float


class LoadReport(VModel):
    calls: int                                             # handed to the pipeline
    finished: int
    failed: int
    wall_seconds: float
    calls_per_minute: float | None = None
    parallel: int
    by_status: dict[str, int] = Field(default_factory=dict)
    review_reasons: dict[str, int] = Field(default_factory=dict)   # why calls still need a person
    straight_through: int = 0                              # no review reason and over the auto-approve bar
    approved: int = 0                                      # auto-approved and written by this run
    latency_p50_seconds: float | None = None               # ingest to review-ready
    latency_p95_seconds: float | None = None
    cost_usd: float = 0.0
    cost_per_call_usd: float | None = None
    projections: list[LoadProjection] = Field(default_factory=list)
    projection_basis: str = ""                             # what the projections assume, e.g. "32 in parallel at 8 s per call"
    errors: list[ErrorCount] = Field(default_factory=list)          # failures outside the pipeline (submit, approve)
    modes: dict[str, str] = Field(default_factory=dict)
    stopped_early: bool = False
    planned: int = 0
    pipeline: RunSnapshot = Field(default_factory=RunSnapshot)


class LoadStatus(VModel):
    state: LoadState = "idle"
    run_id: str | None = None
    request: LoadRequest | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    planned: int = 0
    sent: int = 0                                          # handed to the pipeline
    finished: int = 0                                      # done, successfully or not
    failed: int = 0
    approved: int = 0
    in_flight: int = 0
    queued: int = 0                                        # planned, waiting for a free slot
    parallel: int = 0
    window: int = 0
    estimate: LoadEstimate | None = None
    report: LoadReport | None = None
    error: str | None = None
    stored_calls: int = 0                                  # load-test calls currently in the inbox
    max_calls: int = 0
    pipeline: RunSnapshot = Field(default_factory=RunSnapshot)


@dataclass(frozen=True)
class _Live:
    speech: bool
    language: bool
    model: bool
    crm: bool

    @property
    def billed(self) -> bool:
        return self.speech or self.language or self.model


def _percentile(ordered: list[float], q: float) -> float | None:
    if not ordered:
        return None
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 2)


# ---------------------------------------------------------------------------------------------------- runner
class LoadRunner:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._texts: dict[str, tuple[int, int]] = {}         # fixture -> (billed PII text records, characters)
        self._audio: dict[str, float] = {}                   # fixture -> seconds of recording
        self._state: LoadState = "idle"
        self._reset()

    def _reset(self) -> None:
        self._run_id: str | None = None
        self._request: LoadRequest | None = None
        self._estimate: LoadEstimate | None = None
        self._report: LoadReport | None = None
        self._error: str | None = None
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._began = 0.0
        self._planned = self._sent = self._done = self._failed = self._approved = 0
        self._parallel = self._window = 0
        self._straight = 0
        self._by_status: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._latencies: list[float] = []

    # ------------------------------------------------------------------ reading
    def status(self) -> LoadStatus:
        c = self._c
        waiting = self._state == "running"
        return LoadStatus(
            state=self._state, run_id=self._run_id, request=self._request, started_at=self._started_at,
            finished_at=self._finished_at, planned=self._planned, sent=self._sent, finished=self._done,
            failed=self._failed, approved=self._approved, in_flight=max(self._sent - self._done, 0),
            queued=max(self._planned - self._sent, 0) if waiting else 0, parallel=self._parallel, window=self._window,
            estimate=self._estimate, report=self._report, error=self._error, stored_calls=c.deps.store.count_batch(),
            max_calls=c.settings.load_max_calls, pipeline=c.service.stats.snapshot())

    def _live(self, req: LoadRequest) -> _Live:
        c = self._c
        engine = req.engine or c.deps.default_engine
        return _Live(
            speech=c.modes["transcription"].live and req.via == "audio",
            language=c.modes["pii"].live,
            model=c.modes["llm"].live and engine in ("llm", "hybrid") and engine in c.deps.extractors,
            crm=c.modes["crm"].live)

    def _validate(self, req: LoadRequest) -> None:
        c = self._c
        if req.count > c.settings.load_max_calls:
            raise PipelineError(f"a load test is limited to {c.settings.load_max_calls:,} calls "
                                "(VOICECRM_LOAD_MAX_CALLS)", status=422)
        if req.engine and req.engine not in c.service.engine_names():
            raise PipelineError(f"engine '{req.engine}' is not available: no model is configured", status=422)
        if req.auto_approve and c.modes["crm"].live:
            raise PipelineError("a load test never writes to a live CRM: use the local CRM simulator "
                                "(VOICECRM_CRM_MODE=local) for bulk runs, or leave auto-approve off", status=422)

    def _pool(self, req: LoadRequest) -> list[ScriptedCall]:
        c = self._c
        unknown = [f for f in req.fixtures if f not in c.scripted_calls]
        if unknown:
            raise PipelineError(f"unknown calls: {', '.join(unknown[:5])}", status=422)
        calls = [x for x in c.scripted_calls.values()
                 if (not req.fixtures or x.fixture_id in req.fixtures) and (not req.channels or x.channel in req.channels)]
        if req.via == "audio" and self._live(req).speech:
            calls = [x for x in calls if find_recording(x.fixture_id, c.settings.recordings_dir)]
            if not calls:
                raise PipelineError("no recordings to send through Azure Speech: run scripts\\make_audio.bat once "
                                    "(or pick the transcript input)", status=422)
        if not calls:
            raise PipelineError("no scripted calls match the chosen calls and channels", status=422)
        return calls

    @staticmethod
    def _plan(req: LoadRequest, pool: list[ScriptedCall]) -> list[ScriptedCall]:
        """Every call of the pool once per round, each round shuffled, until `count` calls are planned."""
        rng = random.Random(req.seed)
        plan: list[ScriptedCall] = []
        while len(plan) < req.count:
            round_ = pool[:]
            rng.shuffle(round_)
            plan.extend(round_)
        return plan[:req.count]

    def _default_parallel(self, live: _Live) -> int:
        return max(self._c.deps.max_concurrency, _PARALLEL_LIVE if live.billed else _PARALLEL_MOCK)

    # ------------------------------------------------------------------ estimate
    def _text_stats(self, call: ScriptedCall) -> tuple[int, int]:
        cached = self._texts.get(call.fixture_id)
        if cached is None:
            texts = [s.text for s in ingest_from_scripted(call, via="transcript").segments]
            billed = getattr(self._c.deps.pii, "text_records", None)       # packing bills fewer records than segments
            cached = (int(billed(texts)) if callable(billed) else len(texts), sum(len(t) for t in texts))
            self._texts[call.fixture_id] = cached
        return cached

    def _recording_seconds(self, call: ScriptedCall) -> float:
        seconds = self._audio.get(call.fixture_id)
        if seconds is None:
            found = find_recording(call.fixture_id, self._c.settings.recordings_dir)
            seconds = found.seconds if found and found.seconds else call.duration_minutes * 60.0
            self._audio[call.fixture_id] = seconds
        return seconds

    def _tokens_per_call(self) -> tuple[float, float, str]:
        snap = self._c.service.stats.snapshot()
        model_calls = snap.engines.get("llm", 0) + snap.engines.get("hybrid", 0)
        if model_calls >= _MIN_MEASURED_CALLS and snap.tokens_in:
            return (snap.tokens_in / model_calls, snap.tokens_out / model_calls,
                    f"measured on the last {model_calls} model calls")
        return _DEFAULT_TOKENS_IN, _DEFAULT_TOKENS_OUT, "typical for the hybrid engine"

    def estimate(self, req: LoadRequest) -> LoadEstimate:
        c = self._c
        self._validate(req)
        pool = self._pool(req)
        plan = self._plan(req, pool)
        live = self._live(req)
        engine = req.engine or c.deps.default_engine
        parallel = req.parallel or self._default_parallel(live)

        records = 0
        speech_seconds = 0.0
        for call in plan:
            records += self._text_stats(call)[0]
            if live.speech:
                speech_seconds += self._recording_seconds(call)
        per_in, per_out, token_basis = self._tokens_per_call() if live.model else (0.0, 0.0, "")
        tokens_in, tokens_out = round(per_in * len(plan)), round(per_out * len(plan))

        usage: list[UsageRecord] = []
        if live.speech:
            usage.append(UsageRecord(conversation_id="estimate", stage=Stage.transcribe, service="speech",
                                     provider="azure_speech", audio_seconds=float(speech_seconds)))
        if live.language:
            usage.append(UsageRecord(conversation_id="estimate", stage=Stage.policy, service="language_pii",
                                     provider="azure_language", units=float(records)))
        if live.model:
            usage.append(UsageRecord(conversation_id="estimate", stage=Stage.extract, service="llm", provider="foundry",
                                     model=c.settings.foundry_deployment or "default", tokens_in=tokens_in,
                                     tokens_out=tokens_out))
        cost = c.deps.cost_engine.estimate(usage, c.deps.pricing)

        per_call = _SERVICE_SECONDS["base"] + sum(
            _SERVICE_SECONDS[name] for name, on in (("speech", live.speech), ("language", live.language),
                                                    ("model", live.model)) if on)
        per_second = min(parallel / per_call, _CPU_CALLS_PER_S)
        if req.rate > 0:
            per_second = min(per_second, req.rate)
        seconds = len(plan) / per_second + per_call

        billed = [name for name, on in (("Azure Speech", live.speech), ("Azure AI Language", live.language),
                                        (f"Foundry model ({c.settings.foundry_deployment})", live.model)) if on]
        basis = [f"time: about {per_call:.0f} s per call with {parallel} in parallel, capped near {_CPU_CALLS_PER_S:.0f} "
                 "calls/s on one core"]
        if token_basis:
            basis.insert(0, f"tokens per call: {per_in:,.0f} in / {per_out:,.0f} out ({token_basis})")
        warnings: list[str] = []
        if live.speech and len(pool) < req.count:
            warnings.append(f"the {req.count:,} calls repeat {len(pool)} recordings: Azure Speech is billed for each repeat")
        return LoadEstimate(
            calls=len(plan), via=req.via, engine=engine, parallel=parallel, pool=len(pool),
            speech_hours=round(speech_seconds / 3600.0, 3), pii_records=records if live.language else 0,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=round(cost.total, 4), lines=cost.lines,
            billed=billed, seconds=round(seconds, 0), calls_per_minute=round(per_second * 60, 0),
            basis="; ".join(basis), warnings=warnings, illustrative=cost.illustrative)

    # ------------------------------------------------------------------ control
    async def start(self, req: LoadRequest) -> LoadStatus:
        if self._state in _ACTIVE:
            raise PipelineError("a load test is already running: stop it or wait for it to finish", status=409)
        estimate = self.estimate(req)                         # validates the request (422) before anything is spent
        if estimate.billed and not req.confirm:
            raise PipelineError(f"confirmation required: this run uses billed services ({', '.join(estimate.billed)}), "
                                f"about ${estimate.cost_usd:,.2f} at list prices; resend with confirm=true", status=409)
        pool = self._pool(req)
        run_id = uuid.uuid4().hex[:8]
        self._reset()
        self._stop.clear()
        self._run_id, self._request, self._estimate = run_id, req, estimate
        self._planned, self._parallel, self._state = estimate.calls, estimate.parallel, "preparing"
        self._task = asyncio.create_task(self._run(req, pool, estimate, run_id), name=f"load-{run_id}")
        return self.status()

    async def stop(self) -> LoadStatus:
        """Stop sending. Calls already in the pipeline finish; the ones not yet sent are discarded."""
        if self._state in ("preparing", "running"):
            self._state = "stopping"
            self._stop.set()
        return self.status()

    async def clear(self) -> int:
        """Remove every load-test call from the inbox, vault, state store and (local) CRM simulator."""
        if self._state in _ACTIVE:
            raise PipelineError("a load test is running: stop it first", status=409)
        return await self._remove_batch()

    async def aclose(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (asyncio.TimeoutError, Exception):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _remove_batch(self) -> int:
        c = self._c
        ids = c.deps.store.batch_conversation_ids()
        if not ids:
            return 0
        gate = asyncio.Semaphore(16)

        async def purge(cid: str) -> None:
            async with gate:
                await c.deps.vault.delete(cid, ("working", "audio", "transcript"))

        await asyncio.gather(*(purge(cid) for cid in ids))
        delete_crm = getattr(c.deps.crm, "delete_conversations", None)       # only the local simulator can; never Dataverse
        if callable(delete_crm):
            await delete_crm(ids)
        return c.deps.store.delete_conversations(ids)

    # ------------------------------------------------------------------ the run
    async def _run(self, req: LoadRequest, pool: list[ScriptedCall], estimate: LoadEstimate, run_id: str) -> None:
        c = self._c
        service, pii = c.service, c.deps.pii
        pii_before = getattr(pii, "concurrency", None)
        watchers: set[asyncio.Task[None]] = set()
        try:
            await self._remove_batch()                        # the new run replaces the previous load-test calls
            service.set_concurrency(estimate.parallel)
            if pii_before is not None:
                pii.set_concurrency(max(pii_before, min(estimate.parallel, _MAX_PII_CONCURRENCY)))
            service.stats.begin_batch()
            plan = self._plan(req, pool)
            audio = req.via == "audio"
            self._window = max(estimate.parallel + 4, 8) if audio else max(2 * estimate.parallel, 16)
            recordings: Path | None = c.settings.recordings_dir if self._live(req).speech else None
            self._started_at, self._began = utcnow(), time.monotonic()
            self._state = "stopping" if self._stop.is_set() else "running"

            for index, call in enumerate(plan):
                if not await self._wait_for_turn(req, index):
                    break
                try:
                    ing = ingest_from_scripted(call, via=req.via, asr_noise=req.asr_noise, engine=req.engine,
                                               nonce=f"{run_id}-{index}", recordings_dir=recordings)
                    ing.batch_id = f"load-{run_id}"
                    _, task = await service.start(ing)
                except Exception as exc:
                    self._count_failure(f"submit: {type(exc).__name__}: {str(exc)[:100]}")
                    continue
                self._sent += 1
                if task is None:                              # duplicate source id: cannot happen with a fresh nonce
                    self._done += 1
                    continue
                watcher = asyncio.create_task(self._watch(task, req))
                watchers.add(watcher)
                watcher.add_done_callback(watchers.discard)
                await asyncio.sleep(0)                        # let the pipeline and the API breathe between submissions
            if watchers:
                await asyncio.gather(*watchers)
            stopped = self._stop.is_set()
            self._report = self._build_report(req, estimate, stopped)
            self._state = "stopped" if stopped else "finished"
        except asyncio.CancelledError:
            self._state = "stopped"
            raise
        except Exception as exc:
            logger.exception("load test %s failed", run_id)
            self._error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self._state = "failed"
        finally:
            self._finished_at = utcnow()
            service.set_concurrency(c.deps.max_concurrency)
            if pii_before is not None:
                pii.set_concurrency(pii_before)

    async def _wait_for_turn(self, req: LoadRequest, index: int) -> bool:
        """Waits for this call's arrival time and a free slot. False when a stop was requested meanwhile."""
        if req.rate > 0:
            delay = self._began + index / req.rate - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        while self._sent - self._done >= self._window and not self._stop.is_set():
            await asyncio.sleep(0.02)
        return not self._stop.is_set()

    async def _watch(self, task: asyncio.Task[ConversationRecord], req: LoadRequest) -> None:
        try:
            rec = await asyncio.shield(task)                  # a cancelled watcher must not cancel the call itself
            final = await self._settle(rec, req)
            self._by_status[final.status.value] += 1
            if final.status == ConversationStatus.failed:
                self._failed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed += 1
            self._by_status["failed"] += 1
            self._errors[f"call: {type(exc).__name__}: {str(exc)[:100]}"] += 1
        finally:
            self._done += 1

    async def _settle(self, rec: ConversationRecord, req: LoadRequest) -> ConversationRecord:
        """Tallies a processed call and, when asked, approves it if no human is needed (as the auto-approve policy would)."""
        proposal = rec.proposal
        if proposal and proposal.ready_at:
            self._latencies.append((proposal.ready_at - rec.created_at).total_seconds())
        if rec.status == ConversationStatus.written:
            self._straight += 1                               # already approved by the pipeline's own auto-approve policy
            return rec
        if rec.status != ConversationStatus.needs_review or proposal is None:
            return rec
        review = proposal.review
        self._reasons.update(review.reasons)
        if review.reasons or not review.auto_approve_eligible:
            return rec
        self._straight += 1
        if not req.auto_approve:
            return rec
        try:
            approved = await self._c.service.approve(rec.meta.conversation_id, ApproveRequest(clicks=0), auto=True)
        except Exception as exc:                              # the call stays in the inbox for a person
            self._errors[f"approve: {type(exc).__name__}: {str(exc)[:100]}"] += 1
            return rec
        if approved.status == ConversationStatus.written:
            self._approved += 1
        return approved

    def _count_failure(self, message: str) -> None:
        self._failed += 1
        self._done += 1
        self._sent += 1
        self._by_status["failed"] += 1
        self._errors[message] += 1

    # ------------------------------------------------------------------ report
    def _steady_rate(self, snapshot: RunSnapshot, req: LoadRequest) -> tuple[float, str] | None:
        """Calls a minute the pipeline sustains once it is full: calls in parallel over the time one call takes.

        The measured rate of a short run is dragged down by its ramp-up and tail, which would make a projection to
        thousands of calls pessimistic; the per-call processing time does not have that bias.
        """
        total, wait = snapshot.end_to_end_seconds.mean, snapshot.queue_wait_seconds.mean
        if total is None or wait is None or not self._parallel:
            return None
        service = max(total - wait, 0.05)
        per_second = min(self._parallel / service, _CPU_CALLS_PER_S)
        if req.rate > 0:
            per_second = min(per_second, req.rate)
        return per_second * 60, f"{self._parallel} in parallel at about {service:.1f} s per call"

    def _build_report(self, req: LoadRequest, estimate: LoadEstimate, stopped: bool) -> LoadReport:
        c = self._c
        snapshot = c.service.stats.snapshot()
        wall = max(time.monotonic() - self._began, 0.001)
        rate = self._done / wall * 60 if self._done and wall >= 0.2 else None
        steady = self._steady_rate(snapshot, req)
        projected = steady[0] if steady else rate
        ordered = sorted(self._latencies)
        per_call = snapshot.cost_usd / snapshot.completed if snapshot.completed else None
        return LoadReport(
            calls=self._sent, finished=self._done, failed=self._failed, wall_seconds=round(wall, 1),
            calls_per_minute=round(rate, 1) if rate else None, parallel=self._parallel,
            by_status=dict(self._by_status), review_reasons=dict(self._reasons.most_common()),
            straight_through=self._straight, approved=self._approved,
            latency_p50_seconds=_percentile(ordered, 0.5), latency_p95_seconds=_percentile(ordered, 0.95),
            cost_usd=snapshot.cost_usd, cost_per_call_usd=round(per_call, 5) if per_call is not None else None,
            projections=[LoadProjection(calls=n, seconds=round(n / projected * 60)) for n in _PROJECT_CALLS] if projected else [],
            projection_basis=steady[1] if steady else "the rate measured in this run",
            errors=[ErrorCount(error=e, count=n) for e, n in self._errors.most_common(_TOP_ERRORS)],
            modes={name: mode.effective for name, mode in c.modes.items()}, stopped_early=stopped,
            planned=estimate.calls, pipeline=snapshot)

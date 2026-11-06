"""Operational metrics and the evaluation scoreboard."""
from __future__ import annotations

import statistics
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException

from ...container import Container
from ...domain.enums import ConversationStatus
from ...domain.evaluation import EvalRun
from ...eval.harness import run_eval
from ...pipeline.stats import RunSnapshot
from ..deps import cost_for, get_container
from ..schemas import EvalRunRequest, MetricsSummary

router = APIRouter()


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 2)


@router.get("/metrics/summary", response_model=MetricsSummary)
async def metrics_summary(c: Container = Depends(get_container)) -> MetricsSummary:
    recs = c.deps.store.list_conversations(limit=1000)
    latencies = [(r.proposal.ready_at - r.created_at).total_seconds() for r in recs if r.proposal and r.proposal.ready_at]
    costs = [cost_for(c, r.meta.conversation_id).total for r in recs if r.proposal]
    reviews = [r.proposal.review for r in recs if r.proposal and r.proposal.review.state in ("approved", "auto_approved")]
    manual = [rv for rv in reviews if rv.state == "approved"]
    return MetricsSummary(
        conversations=len(recs), by_status=dict(Counter(r.status.value for r in recs)),
        by_channel=dict(Counter(r.meta.source.value for r in recs)),
        written=sum(r.status == ConversationStatus.written for r in recs),
        needs_review=sum(r.status == ConversationStatus.needs_review for r in recs),
        auto_approved=sum(rv.state == "auto_approved" for rv in reviews),
        latency_p50_seconds=_pct(latencies, 0.5), latency_p95_seconds=_pct(latencies, 0.95),
        mean_cost_usd=round(statistics.mean(costs), 6) if costs else None,
        mean_clicks_to_approve=round(statistics.mean(rv.clicks for rv in manual), 2) if manual else None,
        mean_seconds_to_review=round(statistics.mean(rv.seconds_to_review for rv in manual if rv.seconds_to_review is not None), 1)
        if any(rv.seconds_to_review is not None for rv in manual) else None,
        cost_illustrative=c.deps.pricing.illustrative)


@router.get("/metrics/pipeline", response_model=RunSnapshot)
async def pipeline_stats(c: Container = Depends(get_container)) -> RunSnapshot:
    """Live figures for the current (or most recent) batch: queue, in-flight, throughput, stage latency, tokens, cost."""
    return c.service.stats.snapshot().model_copy(update={"cost_illustrative": c.deps.pricing.illustrative})


@router.post("/eval/run", response_model=EvalRun)
async def eval_run(body: EvalRunRequest, c: Container = Depends(get_container)) -> EvalRun:
    if body.engine and body.engine not in c.service.engine_names():
        raise HTTPException(422, f"engine '{body.engine}' is not available (available: {c.service.engine_names()})")
    run = await run_eval(c, engine=body.engine, with_asr_noise=body.with_asr_noise, apply_learning=body.apply_learning,
                         label=body.label)
    c.deps.store.save_eval_run(run)
    return run


@router.get("/eval/runs", response_model=list[EvalRun])
async def eval_runs(c: Container = Depends(get_container)) -> list[EvalRun]:
    return [r.model_copy(update={"cases": []}) for r in c.deps.store.list_eval_runs()]


@router.get("/eval/runs/{run_id}", response_model=EvalRun)
async def eval_run_get(run_id: str, c: Container = Depends(get_container)) -> EvalRun:
    run = c.deps.store.get_eval_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run

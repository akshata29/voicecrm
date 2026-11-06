"""Evaluation (scoreboard) models."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .base import VModel
from .conversation import utcnow


class EvalCaseResult(VModel):
    fixture_id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    product_top1_correct: int = 0              # gold products whose exact product_id (incl. share class) was chosen
    product_top3_hits: int = 0                 # gold products found in the candidate top-3
    product_gold: int = 0
    product_false_tags: int = 0                # chosen products not in gold
    share_class_confusions: int = 0            # right fund, wrong share class
    topic_tp: int = 0
    topic_fp: int = 0
    topic_fn: int = 0
    task_tp: int = 0
    task_fp: int = 0
    task_fn: int = 0
    leaks: int = 0                             # must_not_leak strings found in CRM-bound fields
    kept_relationship: int = 0
    kept_relationship_gold: int = 0
    wer: float | None = None                   # transcription word error rate vs the script (when transcribed)
    latency_ms: int | None = None
    cost_usd: float | None = None
    needs_review: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class EvalMetrics(VModel):
    cases: int = 0
    product_top1: float = 0.0
    product_top3: float = 0.0
    product_false_tag_rate: float = 0.0
    share_class_confusion_rate: float = 0.0
    topic_precision: float = 0.0
    topic_recall: float = 0.0
    topic_f1: float = 0.0
    task_precision: float = 0.0
    task_recall: float = 0.0
    privacy_leaks: int = 0
    relationship_kept_rate: float = 0.0
    mean_wer: float | None = None
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    mean_cost_usd: float | None = None
    auto_approvable_rate: float = 0.0


class EvalRun(VModel):
    run_id: str
    created_at: datetime = Field(default_factory=utcnow)
    engine: str
    with_asr_noise: bool = False
    learned_aliases: int = 0                   # aliases applied before the run (before/after learning)
    label: str | None = None
    metrics: EvalMetrics
    cases: list[EvalCaseResult]

"""Live pipeline statistics for the current batch: queue depth, per-stage latency, throughput, tokens, failures.

The state store is the durable record of every conversation. This keeps what someone watching a bulk load needs at a
glance - how many calls are queued, in flight and done, calls per minute, p50/p95 per stage, tokens, an ETA - in memory,
so polling it never scans the database.

A "run" is a burst of work: it starts when a call is submitted after the pipeline has been idle for a few seconds, and
ends when the last call finishes. Every figure describes the current (or most recent) run, so a bulk load reads as one
batch even when calls trickle in faster than they finish. Nothing here carries transcript text; error strings are the
pipeline's already-sanitised messages.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from datetime import datetime

from pydantic import Field

from ..domain.base import VModel
from ..domain.conversation import utcnow

_WINDOW = 5000                 # samples kept per series
_RATE_WINDOW_S = 60.0          # trailing window for the "recent calls per minute" figure
_MIN_RATE_SPAN_S = 5.0         # below this a rate is noise
_RUN_GAP_S = 10.0              # a pause shorter than this does not end a batch (arrivals trickling in faster than they finish)
_TOP_ERRORS = 5


class SeriesSummary(VModel):
    count: int = 0
    mean: float | None = None
    p50: float | None = None
    p95: float | None = None
    max: float | None = None
    errors: int = 0


class ErrorCount(VModel):
    error: str
    count: int


class RunSnapshot(VModel):
    active: bool = False
    started_at: datetime | None = None
    max_concurrency: int = 0
    submitted: int = 0
    queued: int = 0
    in_flight: int = 0
    completed: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0
    calls_per_minute: float | None = None             # finished calls / elapsed time of the run
    recent_calls_per_minute: float | None = None      # finished calls in the trailing minute
    eta_seconds: float | None = None
    queue_wait_seconds: SeriesSummary = Field(default_factory=SeriesSummary)
    end_to_end_seconds: SeriesSummary = Field(default_factory=SeriesSummary)
    stages_ms: dict[str, SeriesSummary] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0                              # estimated from the pricing table (see cost_illustrative)
    cost_illustrative: bool = True
    engines: dict[str, int] = Field(default_factory=dict)
    fallbacks: int = 0
    errors: list[ErrorCount] = Field(default_factory=list)
    lifetime_completed: int = 0
    lifetime_failed: int = 0


class _Series:
    __slots__ = ("values", "errors", "total", "count")

    def __init__(self) -> None:
        self.values: deque[float] = deque(maxlen=_WINDOW)
        self.errors = 0
        self.total = 0.0
        self.count = 0

    def add(self, value: float) -> None:
        self.values.append(value)
        self.total += value
        self.count += 1

    def summary(self, digits: int = 2) -> SeriesSummary:
        if not self.values:
            return SeriesSummary(errors=self.errors)
        ordered = sorted(self.values)
        last = len(ordered) - 1

        def at(q: float) -> float:
            return round(ordered[min(last, int(round(q * last)))], digits)

        return SeriesSummary(count=self.count, mean=round(self.total / self.count, digits), p50=at(0.5), p95=at(0.95),
                             max=round(ordered[-1], digits), errors=self.errors)


class PipelineStats:
    def __init__(self, *, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self.lifetime_completed = 0
        self.lifetime_failed = 0
        self._clock = time.monotonic
        self._begin_run()

    # ------------------------------------------------------------------ run lifecycle
    def _begin_run(self) -> None:
        self._submitted = self._started = self._completed = self._failed = 0
        self._began: float | None = None
        self._began_at: datetime | None = None
        self._last_finish: float | None = None
        self._finishes: deque[float] = deque()
        self._queue_wait = _Series()
        self._end_to_end = _Series()
        self._stages: dict[str, _Series] = {}
        self._tokens_in = self._tokens_out = self._fallbacks = 0
        self._cost_usd = 0.0
        self._engines: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()

    @property
    def _unfinished(self) -> int:
        return self._submitted - self._completed - self._failed

    def begin_batch(self) -> None:
        """Start a fresh batch now, so a run launched from the UI never inherits the previous run's figures."""
        if self._unfinished <= 0:
            self._begin_run()

    def reset(self) -> None:
        """Forget every figure, lifetime totals included (a demo reset). Only meaningful when nothing is in flight."""
        self.lifetime_completed = self.lifetime_failed = 0
        self._begin_run()

    # ------------------------------------------------------------------ recording (called by the pipeline)
    def submitted(self) -> None:
        idle_for = self._clock() - self._last_finish if self._last_finish is not None else float("inf")
        if self._unfinished <= 0 and self._submitted and idle_for >= _RUN_GAP_S:
            self._begin_run()
        if self._began is None:
            self._began, self._began_at = self._clock(), utcnow()
        self._submitted += 1

    def started(self, waited_seconds: float) -> None:
        self._started += 1
        self._queue_wait.add(max(waited_seconds, 0.0))

    def stage(self, stage: str, milliseconds: float, *, ok: bool = True) -> None:
        series = self._stages.setdefault(stage, _Series())
        if ok:
            series.add(milliseconds)
        else:
            series.errors += 1

    def extracted(self, engine: str, tokens_in: int, tokens_out: int, *, fallback: bool) -> None:
        self._tokens_in += tokens_in
        self._tokens_out += tokens_out
        self._engines[engine] += 1
        self._fallbacks += int(fallback)

    def finished(self, *, ok: bool, total_seconds: float, error: str | None = None, cost_usd: float = 0.0) -> None:
        now = self._clock()
        self._last_finish = now
        self._cost_usd += cost_usd
        if ok:
            self._completed += 1
            self.lifetime_completed += 1
            self._end_to_end.add(max(total_seconds, 0.0))
            self._finishes.append(now)
            while self._finishes and now - self._finishes[0] > 2 * _RATE_WINDOW_S:
                self._finishes.popleft()
        else:
            self._failed += 1
            self.lifetime_failed += 1
            self._end_to_end.errors += 1
            self._errors[(error or "unknown error")[:160]] += 1

    # ------------------------------------------------------------------ reading
    def snapshot(self) -> RunSnapshot:
        now = self._clock()
        active = self._unfinished > 0
        began = self._began
        end = now if active else (self._last_finish or now)
        elapsed = max(end - began, 0.0) if began is not None else 0.0
        done = self._completed + self._failed
        overall = (done / elapsed * 60) if elapsed >= _MIN_RATE_SPAN_S and done else None
        recent = None
        if began is not None and done:
            span = min(_RATE_WINDOW_S, max(now - began, 0.0)) if active else min(_RATE_WINDOW_S, elapsed)
            if span >= _MIN_RATE_SPAN_S:
                cutoff = end - span
                recent = round(sum(1 for t in self._finishes if t >= cutoff) / span * 60, 1)
        rate = recent if (recent and elapsed > 2 * _MIN_RATE_SPAN_S) else overall
        eta = round(self._unfinished / (rate / 60), 0) if active and rate else None
        return RunSnapshot(
            active=active, started_at=self._began_at, max_concurrency=self.max_concurrency,
            submitted=self._submitted, queued=max(self._submitted - self._started, 0),
            in_flight=max(self._started - self._completed - self._failed, 0), completed=self._completed, failed=self._failed,
            elapsed_seconds=round(elapsed, 1), calls_per_minute=round(overall, 1) if overall else None,
            recent_calls_per_minute=recent, eta_seconds=eta,
            queue_wait_seconds=self._queue_wait.summary(), end_to_end_seconds=self._end_to_end.summary(),
            stages_ms={name: series.summary(0) for name, series in self._stages.items()},
            tokens_in=self._tokens_in, tokens_out=self._tokens_out, cost_usd=round(self._cost_usd, 4),
            engines=dict(self._engines), fallbacks=self._fallbacks,
            errors=[ErrorCount(error=e, count=n) for e, n in self._errors.most_common(_TOP_ERRORS)],
            lifetime_completed=self.lifetime_completed, lifetime_failed=self.lifetime_failed,
        )

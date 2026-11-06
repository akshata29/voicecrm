"""Golden-set evaluation: runs every scripted call through the real pipeline in an isolated container and scores it.

'apply_learning' demonstrates the improvement loop: a baseline pass finds product mentions the system saw but
mis-resolved, a simulated reviewer correction is recorded for each (surface -> gold product), and the measured run
uses those learned aliases. It is measured on the same calls, so it shows the mechanism, not held-out accuracy.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from rapidfuzz import fuzz

from ..container import Container
from ..domain.catalogue import LearnedAlias
from ..domain.enums import StageStatus
from ..domain.evaluation import EvalCaseResult, EvalRun
from ..domain.fixtures import ScriptedCall
from ..domain.pipeline import ConversationRecord
from ..pipeline.fixtures import ingest_from_scripted
from ..pipeline.service import normalize_surface
from .metrics import aggregate, score_case

logger = logging.getLogger(__name__)


async def _run_calls(c: Container, calls: list[ScriptedCall], engine: str, noise: bool) -> list[ConversationRecord]:
    async def one(call: ScriptedCall) -> ConversationRecord:
        ing = ingest_from_scripted(call, via="transcript", asr_noise=noise, engine=engine, nonce="eval")
        return await c.service.run_to_completion(ing)

    return list(await asyncio.gather(*(one(call) for call in calls)))


def _derive_aliases(calls: list[ScriptedCall], recs: list[ConversationRecord]) -> list[LearnedAlias]:
    """Simulated reviewer corrections: for each non-ambiguous gold product that was not chosen, alias the closest mention."""
    aliases: dict[tuple[str, str], LearnedAlias] = {}
    for call, rec in zip(calls, recs, strict=True):
        if rec.proposal is None:
            continue
        mentions = rec.proposal.result.products
        chosen = {m.chosen_product_id for m in mentions if m.chosen_product_id}
        for gold in call.gold.products:
            if gold.ambiguous or gold.product_id in chosen:
                continue
            best, best_score = None, 0.0
            for m in mentions:
                if m.chosen_product_id and m.chosen_product_id in {g.product_id for g in call.gold.products}:
                    continue
                score = max([fuzz.token_set_ratio(m.surface.lower(), f.lower()) for f in gold.surface_forms] or [0.0])
                if gold.product_id in {cand.product_id for cand in m.candidates}:
                    score += 30
                if score > best_score:
                    best, best_score = m, score
            if best is not None and best_score >= 60:
                surface = normalize_surface(best.surface)
                if surface:
                    aliases[(surface, gold.product_id)] = LearnedAlias(surface=surface, product_id=gold.product_id,
                                                                       source="simulated_reviewer_correction")
    return list(aliases.values())


async def run_eval(
    container: Container, *, engine: str | None = None, with_asr_noise: bool = False, apply_learning: bool = False,
    label: str | None = None,
) -> EvalRun:
    calls = list(container.scripted_calls.values())
    engine_name = engine or container.deps.default_engine
    family_of = {p.product_id: p.family_id for p in container.deps.catalogue.products}
    iso = container.isolated()
    learned = 0
    try:
        if apply_learning:
            base = container.isolated()
            try:
                derived = _derive_aliases(calls, await _run_calls(base, calls, engine_name, with_asr_noise))
            finally:
                base.close()
            for alias in [*container.deps.store.list_aliases(), *derived]:
                iso.deps.store.upsert_alias(alias)
            iso.service.reload_aliases()
            learned = len(iso.deps.store.list_aliases())
        recs = await _run_calls(iso, calls, engine_name, with_asr_noise)
        cases: list[EvalCaseResult] = []
        for call, rec in zip(calls, recs, strict=True):
            cid = rec.meta.conversation_id
            cost = iso.deps.cost_engine.estimate(iso.deps.store.list_usage(cid), iso.deps.pricing).total
            case = score_case(call, rec, family_of=family_of, cost_usd=cost)
            busy = sum(e.duration_ms or 0 for e in iso.deps.store.list_events(cid) if e.status == StageStatus.completed)
            case.latency_ms = busy or case.latency_ms          # processing time per call, without queueing behind other calls
            cases.append(case)
        run_label = label or (("learned" if apply_learning else "baseline") + (" + ASR noise" if with_asr_noise else ""))
        return EvalRun(run_id=uuid4().hex[:10], engine=engine_name, with_asr_noise=with_asr_noise, learned_aliases=learned,
                       label=run_label, metrics=aggregate(cases), cases=cases)
    finally:
        iso.close()

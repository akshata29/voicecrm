"""Scoring of a processed scripted call against its gold labels, and aggregation into scoreboard metrics."""
from __future__ import annotations

import json
import re
import statistics

from ..domain.evaluation import EvalCaseResult, EvalMetrics
from ..domain.fixtures import ScriptedCall
from ..domain.pipeline import ConversationRecord

_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text.lower()).strip()


def _digits_only(text: str) -> str:
    return re.sub(r"[\s\-]", "", text)


def contains_leak(haystack: str, needle: str) -> bool:
    h, n = _norm(haystack), _norm(needle)
    if n and n in h:
        return True
    if sum(c.isdigit() for c in needle) >= 8:
        return _digits_only(needle) in _digits_only(haystack)
    return False


def _task_text(title: str, description: str | None) -> str:
    return f"{title} {description or ''}".lower()


def score_case(
    call: ScriptedCall,
    rec: ConversationRecord,
    *,
    family_of: dict[str, str],
    cost_usd: float | None = None,
) -> EvalCaseResult:
    gold, prop = call.gold, rec.proposal
    nonamb = [g for g in gold.products if not g.ambiguous]
    base = EvalCaseResult(
        fixture_id=call.fixture_id, title=call.title, tags=call.tags, product_gold=len(gold.products),
        kept_relationship_gold=len(gold.must_keep), cost_usd=cost_usd,
        details={"gold_nonambiguous": len(nonamb), "status": rec.status.value},
    )
    if prop is None:
        base.topic_fn, base.task_fn = len(gold.topics), len(gold.tasks)
        base.details["error"] = rec.error or "no proposal"
        return base

    result = prop.result
    chosen = {m.chosen_product_id for m in result.products if m.chosen_product_id}
    candidates = {c.product_id for m in result.products for c in m.candidates} | chosen
    top1 = top3 = 0
    for g in gold.products:
        if g.ambiguous:
            if (set(g.acceptable_ids) or {g.product_id}) & candidates:
                top3 += 1
        else:
            top1 += g.product_id in chosen
            top3 += g.product_id in candidates
    gold_ids = {g.product_id for g in gold.products} | {a for g in gold.products for a in g.acceptable_ids}
    gold_families = {family_of[g.product_id] for g in nonamb if g.product_id in family_of}
    wrong = [c for c in chosen if c not in gold_ids]
    base.product_top1_correct, base.product_top3_hits = top1, top3
    base.product_false_tags = len(wrong)
    base.share_class_confusions = sum(1 for c in wrong if family_of.get(c) in gold_families)

    pred_topics, gold_topics = {t.topic_id for t in result.topics}, set(gold.topics)
    base.topic_tp, base.topic_fp, base.topic_fn = (
        len(pred_topics & gold_topics), len(pred_topics - gold_topics), len(gold_topics - pred_topics))

    unmatched = list(result.tasks)
    tp = 0
    for g in gold.tasks:
        hit = next((t for t in unmatched if all(k.lower() in _task_text(t.title, t.description) for k in g.keywords)
                    and (g.due_date is None or t.due_date == g.due_date)), None)
        if hit is not None:
            tp += 1
            unmatched.remove(hit)
    base.task_tp, base.task_fn, base.task_fp = tp, len(gold.tasks) - tp, len(unmatched)

    haystack = prop.model_dump_json()
    base.leaks = sum(contains_leak(haystack, needle) for needle in gold.must_not_leak)
    keep_text = _norm(prop.note_markdown + " " + " ".join(f.text for f in result.relationship_facts))
    base.kept_relationship = sum(k.lower() in keep_text for k in gold.must_keep)

    if prop.ready_at:
        base.latency_ms = int((prop.ready_at - rec.created_at).total_seconds() * 1000)
    base.needs_review = bool(prop.review.reasons)
    base.details.update(
        engine=result.engine.value, template=prop.template_id, review_reasons=prop.review.reasons,
        crm_account=prop.crm_match.account.id if prop.crm_match.account else None,
        crm_expected=gold.expect_crm_account,
        crm_ok=(gold.expect_crm_account is None
                or (prop.crm_match.account is not None and prop.crm_match.account.id == gold.expect_crm_account)),
        auto_approvable=bool(prop.review.auto_approve_eligible) or (not prop.review.reasons and result.overall_confidence >= 0.9),
    )
    return base


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def aggregate(cases: list[EvalCaseResult]) -> EvalMetrics:
    nonamb = sum(int(c.details.get("gold_nonambiguous", 0)) for c in cases)
    top1 = sum(c.product_top1_correct for c in cases)
    top3 = sum(c.product_top3_hits for c in cases)
    gold = sum(c.product_gold for c in cases)
    chosen_or_gold = gold + sum(c.product_false_tags for c in cases)
    tp, fp, fn = (sum(c.topic_tp for c in cases), sum(c.topic_fp for c in cases), sum(c.topic_fn for c in cases))
    prec, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    ttp, tfp, tfn = (sum(c.task_tp for c in cases), sum(c.task_fp for c in cases), sum(c.task_fn for c in cases))
    lat = [c.latency_ms for c in cases if c.latency_ms is not None]
    costs = [c.cost_usd for c in cases if c.cost_usd is not None]
    wers = [c.wer for c in cases if c.wer is not None]
    kept_gold = sum(c.kept_relationship_gold for c in cases)
    return EvalMetrics(
        cases=len(cases),
        product_top1=_ratio(top1, nonamb),
        product_top3=_ratio(top3, gold),
        product_false_tag_rate=_ratio(sum(c.product_false_tags for c in cases), chosen_or_gold),
        share_class_confusion_rate=_ratio(sum(c.share_class_confusions for c in cases), max(gold, 1)),
        topic_precision=prec,
        topic_recall=rec,
        topic_f1=round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
        task_precision=_ratio(ttp, ttp + tfp),
        task_recall=_ratio(ttp, ttp + tfn),
        privacy_leaks=sum(c.leaks for c in cases),
        relationship_kept_rate=_ratio(sum(c.kept_relationship for c in cases), kept_gold) if kept_gold else 1.0,
        mean_wer=round(statistics.mean(wers), 4) if wers else None,
        latency_p50_ms=_pct(lat, 0.5),
        latency_p95_ms=_pct(lat, 0.95),
        mean_cost_usd=round(statistics.mean(costs), 6) if costs else None,
        auto_approvable_rate=_ratio(sum(bool(c.details.get("auto_approvable")) for c in cases), len(cases)),
    )


def summarize_cases_json(cases: list[EvalCaseResult]) -> str:
    return json.dumps([c.model_dump(mode="json") for c in cases], indent=1)

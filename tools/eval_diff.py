"""Developer tool: run the golden set and print, per scripted call, exactly where the output differs from the gold labels.

    cd backend
    ..\\.venv\\Scripts\\python.exe ..\\tools\\eval_diff.py [--noise] [--engine rules] [fixture_id ...]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from voicecrm.container import build_container  # noqa: E402
from voicecrm.pipeline.fixtures import ingest_from_scripted  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixtures", nargs="*")
    ap.add_argument("--noise", action="store_true")
    ap.add_argument("--engine", default="rules")
    ap.add_argument("--note", action="store_true", help="also print the rendered note")
    args = ap.parse_args()
    container = build_container().isolated()
    products = container.deps.catalogue.by_id()
    try:
        for call in container.scripted_calls.values():
            if args.fixtures and call.fixture_id not in args.fixtures:
                continue
            rec = await container.service.run_to_completion(
                ingest_from_scripted(call, via="transcript", asr_noise=args.noise, engine=args.engine, nonce="dbg"))
            prop = rec.proposal
            print(f"\n=== {call.fixture_id}  status={rec.status.value}")
            if prop is None:
                print("   NO PROPOSAL:", rec.error)
                continue
            result, gold = prop.result, call.gold
            chosen = {m.chosen_product_id for m in result.products if m.chosen_product_id}
            for g in gold.products:
                got = g.product_id in chosen
                cands = {c.product_id for m in result.products for c in m.candidates}
                print(f"   gold product {products[g.product_id].display_name:<48} {'OK ' if got else ('top3' if g.product_id in cands else 'MISS')}"
                      f"{' (ambiguous)' if g.ambiguous else ''}")
            gold_ids = {g.product_id for g in gold.products} | {a for g in gold.products for a in g.acceptable_ids}
            for m in result.products:
                if m.chosen_product_id and m.chosen_product_id not in gold_ids:
                    print(f"   EXTRA product chosen: {products[m.chosen_product_id].display_name}   surface={m.surface!r}")
                if m.chosen_product_id is None:
                    print(f"   needs pick: {m.surface!r} reasons={m.reasons} cands={[c.share_class for c in m.candidates]}")
            pred_topics = {t.topic_id for t in result.topics}
            print(f"   topics  missing={sorted(set(gold.topics) - pred_topics)}  extra={sorted(pred_topics - set(gold.topics))}")
            for g in gold.tasks:
                hit = [t for t in result.tasks if all(k in f"{t.title} {t.description or ''}".lower() for k in g.keywords)]
                ok = [t for t in hit if g.due_date is None or t.due_date == g.due_date]
                if not ok:
                    print(f"   gold task MISSING {g.keywords} due={g.due_date}; same-keyword tasks: {[(t.title, t.due_date) for t in hit]}")
            for t in result.tasks:
                if not any(all(k in f"{t.title} {t.description or ''}".lower() for k in g.keywords) for g in gold.tasks):
                    print(f"   EXTRA task: {t.title!r} due={t.due_date}  <- {t.evidence[0].quote[:90]!r}")
            for k in gold.must_keep:
                present = k.lower() in (prop.note_markdown + " ".join(f.text for f in result.relationship_facts)).lower()
                print(f"   must_keep {k!r}: {'kept' if present else 'MISSING'}")
            print(f"   review reasons={prop.review.reasons} crm={prop.crm_match.account.id if prop.crm_match.account else None} "
                  f"expected={gold.expect_crm_account} overall={result.overall_confidence}")
            if args.note:
                print(prop.note_markdown)
    finally:
        container.close()


if __name__ == "__main__":
    asyncio.run(main())

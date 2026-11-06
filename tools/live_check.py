"""Rehearses the live audio path before a demo: real recordings -> Azure Speech -> Azure Language -> model -> scored.

    cd backend
    ..\\.venv\\Scripts\\python.exe ..\\tools\\live_check.py                              every recorded call, 2 at a time
    ..\\.venv\\Scripts\\python.exe ..\\tools\\live_check.py pb-teams-equity-class-c      one call, with the full detail
    ..\\.venv\\Scripts\\python.exe ..\\tools\\live_check.py --engine llm --concurrency 4
    ..\\.venv\\Scripts\\python.exe ..\\tools\\live_check.py --meeting -v                 the Teams meeting recordings (data\\audio\\meeting)
    ..\\.venv\\Scripts\\python.exe ..\\tools\\live_check.py --meeting --via transcript    skip Speech: score the extraction on the script text

Runs in-process with the settings in .env and a throwaway state directory (your .state is never touched). Each call is
processed exactly as the UI's "Run via audio" does it, then scored against the call's gold labels: word error rate of
the transcript, products / topics / tasks, privacy leaks, and per-stage timings. Exits non-zero if a call fails or a
must-not-leak string reaches the proposal, so it can gate a rehearsal. Create the recordings first with gen_call_audio.py
(or gen_meeting_audio.py for --meeting). Meeting scripts are scored as one-microphone Teams calls, exactly as the live
Teams path sees them, and the dates in their gold labels are relative to the script's meeting date (a Friday).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

_WORD = re.compile(r"[\w']+", re.UNICODE)


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(hyp) + 1):
            cur = row[j]
            row[j] = min(row[j] + 1, row[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return row[len(hyp)] / max(len(ref), 1)


def stage_ms(events: list) -> dict[str, int]:
    return {e.stage.value: e.duration_ms for e in events if e.duration_ms is not None and e.status.value == "completed"}


async def run(args: argparse.Namespace) -> int:
    from voicecrm.adapters.sources.recordings import find_recording
    from voicecrm.config import get_settings
    from voicecrm.container import build_container
    from voicecrm.eval.metrics import aggregate, score_case
    from voicecrm.pipeline.fixtures import ingest_from_scripted

    settings = get_settings()
    container = build_container(settings)
    await container.startup()
    try:
        if args.meeting:
            import gen_meeting_audio as meeting

            calls = {path.stem: meeting.to_scripted_call(script, path.stem) for path, script in meeting.load_scripts()}
            recordings_dir = meeting.TOOLS.parent / "data" / "audio" / "meeting"
            maker = "scripts\\make_meeting_audio.bat"
        else:
            calls = container.scripted_calls
            recordings_dir = settings.recordings_dir
            maker = "..\\.venv\\Scripts\\python.exe ..\\tools\\gen_call_audio.py"
        audio = args.via == "audio"

        def has_audio(fixture_id: str) -> bool:
            return find_recording(fixture_id, recordings_dir) is not None

        wanted = args.fixtures or [f for f in calls if has_audio(f) or (args.meeting and not audio)]
        missing = [f for f in wanted if f not in calls or (audio and not has_audio(f))]
        if missing:
            print(f"no recording for: {', '.join(missing)}\n  create them with: {maker}")
            wanted = [f for f in wanted if f not in missing]
        if not wanted:
            return 2
        print(f"transcription={settings.transcription_mode}  llm={settings.llm_mode}  pii={settings.pii_mode}  "
              f"engine={args.engine or container.deps.default_engine}  via={args.via}  concurrency={args.concurrency}  "
              f"calls={len(wanted)}\n")
        family_of = {p.product_id: p.family_id for p in container.deps.catalogue.products}
        gate = asyncio.Semaphore(args.concurrency)

        async def one(fixture_id: str):
            call = calls[fixture_id]
            ing = ingest_from_scripted(call, via=args.via, nonce=uuid4().hex[:5], engine=args.engine,
                                       recordings_dir=recordings_dir)
            async with gate:
                t0 = time.perf_counter()
                rec = await container.service.run_to_completion(ing)
                took = time.perf_counter() - t0
            cid = rec.meta.conversation_id
            stages = stage_ms(container.deps.store.list_events(cid))
            conv, _, _ = await container.service.get_transcript(cid)
            heard = " ".join(s.text for s in conv.segments) if conv else ""
            cost = container.deps.cost_engine.estimate(container.deps.store.list_usage(cid), container.deps.pricing).total
            case = score_case(call, rec, family_of=family_of, cost_usd=cost)
            case.wer = wer(" ".join(t.text for t in call.turns), heard) if heard else None
            return call, rec, case, stages, took, conv

        results = await asyncio.gather(*(one(f) for f in wanted))
        header = f"{'call':<36} {'status':<13} {'total':>6} {'stt':>6} {'pii':>6} {'llm':>6} {'WER':>6}  {'products':<9} {'topics':<10} {'tasks':<10} leaks  review"
        print(header)
        print("-" * len(header))
        failed = 0
        for call, rec, case, stages, took, _ in results:
            bad = rec.status.value == "failed"
            failed += bad
            prod = f"{case.product_top3_hits}/{case.product_gold}" + (f"+{case.product_false_tags}fp" if case.product_false_tags else "")
            topics = f"P{case.topic_tp}/{case.topic_tp + case.topic_fp} R{case.topic_tp}/{case.topic_tp + case.topic_fn}"
            tasks = f"P{case.task_tp}/{case.task_tp + case.task_fp} R{case.task_tp}/{case.task_tp + case.task_fn}"
            reasons = ",".join(rec.proposal.review.reasons) if rec.proposal and rec.proposal.review.reasons else "-"
            wer_text = f"{case.wer:.1%}" if case.wer is not None else "-"
            print(f"{call.fixture_id:<36} {rec.status.value:<13} {took:>5.1f}s {stages.get('transcribe', 0) / 1000:>5.1f}s "
                  f"{stages.get('policy', 0) / 1000:>5.1f}s {stages.get('extract', 0) / 1000:>5.1f}s {wer_text:>6}  {prod:<9} "
                                    f"{topics:<10} {tasks:<10} {case.leaks:>5}  {reasons}")
            if bad:
                print(f"    FAILED: {rec.error}")
        cases = [r[2] for r in results if r[1].proposal]
        if cases:
            m = aggregate(cases)
            print(f"\nacross {m.cases} calls: product top-1 {m.product_top1:.0%} / top-3 {m.product_top3:.0%}, "
                  f"topics P {m.topic_precision:.0%} R {m.topic_recall:.0%}, tasks P {m.task_precision:.0%} R {m.task_recall:.0%}, "
                  f"privacy leaks {m.privacy_leaks}, mean WER {m.mean_wer:.1%}" if m.mean_wer is not None else "")
        if len(results) == 1 or args.verbose:
            for call, rec, _case, _, _, conv in results:
                print(f"\n=== {call.fixture_id}: {call.title}")
                if rec.proposal:
                    prop = rec.proposal
                    print(f"engine={rec.engine} language={rec.language} auto-approve={prop.review.auto_approve_eligible} "
                          f"confidence={prop.result.overall_confidence}  review={prop.review.reasons or 'none'}")
                    for mention in prop.result.products:
                        print(f"  product  {mention.surface!r} -> {mention.chosen_product_id} ({mention.confidence:.2f}) "
                              f"{mention.mention_type.value}")
                    print(f"  topics   {[t.topic_id for t in prop.result.topics]}  (gold {call.gold.topics})")
                    for task in prop.result.tasks:
                        print(f"  task     {task.title}  due={task.due_date}")
                    print(f"  gold     {[(' '.join(g.keywords), str(g.due_date)) for g in call.gold.tasks]}")
                    contact = prop.crm_match.contact.name if prop.crm_match.contact else None
                    print(f"  crm      {prop.crm_match.account.name if prop.crm_match.account else None} / {contact} "
                          f"({prop.crm_match.method})")
                    kinds = Counter(f"{a.kind}:{a.category}" for a in prop.policy_actions)
                    print(f"  policy   {dict(kinds)}  relationship facts: {[f.text for f in prop.result.relationship_facts]}")
                if conv:
                    print("  transcript starts:")
                    for seg in conv.segments[:4]:
                        print(f"    [{seg.speaker_id}] {seg.text[:140]}")
        if args.json:
            Path(args.json).write_text(json.dumps([r[2].model_dump(mode="json") for r in results], indent=1), encoding="utf-8")
        leaks = sum(r[2].leaks for r in results)
        return 1 if failed or leaks else 0
    finally:
        await container.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Rehearse the live audio path against the real Azure services.")
    parser.add_argument("fixtures", nargs="*", help="fixture ids (default: every call that has a recording)")
    parser.add_argument("--engine", choices=["rules", "llm", "hybrid"], default=None, help="extraction engine (default: from .env)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--meeting", action="store_true", help="use the Teams meeting scripts and recordings in data\\meeting and data\\audio\\meeting")
    parser.add_argument("--via", choices=["audio", "transcript"], default="audio",
                        help="audio = Azure Speech on the recording (default); transcript = score the extraction on the script text")
    parser.add_argument("-v", "--verbose", action="store_true", help="show what was extracted for every call")
    parser.add_argument("--json", help="write the per-call scores to this file")
    args = parser.parse_args()
    state = tempfile.mkdtemp(prefix="voicecrm-livecheck-")
    os.environ["VOICECRM_STATE_DIR"] = state
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    finally:
        shutil.rmtree(state, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

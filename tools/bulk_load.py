"""Bulk load: sends hundreds of calls through the running API, shows the pipeline working through them, and reports
what it took - throughput, latency per stage, tokens, estimated cost, and how many calls still need a human.

    .venv\\Scripts\\python.exe tools\\bulk_load.py --count 800                  800 calls as one burst (transcripts)
    .venv\\Scripts\\python.exe tools\\bulk_load.py --count 800 --rate 10        steady arrivals, 10 calls a second
    .venv\\Scripts\\python.exe tools\\bulk_load.py --count 40 --via audio       real recordings through Azure Speech
    .venv\\Scripts\\python.exe tools\\bulk_load.py --count 800 --approve clean  also approve the calls that need no human
    .venv\\Scripts\\python.exe tools\\bulk_load.py --dry-run --count 800        show the plan and the sizing, send nothing

It drives the same HTTP ingest path the UI and the platform webhooks use (POST /ingest/simulate), so every call is
transcribed, privacy-filtered, summarised and matched exactly as in a real run; the 18 scripted calls are cycled with
unique ids so none is de-duplicated. Watch the UI Inbox fill while it runs. For a clean slate and more parallelism start the
API with scripts\\run_bulk_backend.bat. With live Azure services enabled it asks before spending (see --yes).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

API = "/api/v1"
TOKENS_PER_HYBRID_CALL = 3900        # measured on the live hybrid engine: ~3,000 in + ~900 out per call
CLEAR_LINE = "\x1b[2K"


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest:02d} s"


def ms(value: float | None) -> str:
    return "-" if value is None else (f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms")


def tokens(value: float) -> str:
    return f"{value / 1e6:.2f}M" if value >= 1e6 else f"{value / 1e3:.0f}k" if value >= 1e4 else f"{value:.0f}"


@dataclass
class Run:
    plan: list[dict[str, Any]]
    window: int
    base_completed: int = 0
    base_failed: int = 0
    submitted: int = 0
    duplicates: int = 0
    submit_failures: int = 0
    submit_ms: list[float] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    errors: Counter = field(default_factory=Counter)
    began: float | None = None
    submit_done: float | None = None
    finished_at: float | None = None
    latest: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> int:
        return self.latest.get("lifetime_completed", 0) - self.base_completed

    @property
    def failed(self) -> int:
        return self.latest.get("lifetime_failed", 0) - self.base_failed

    @property
    def finished(self) -> int:
        return self.completed + self.failed

    def outstanding(self) -> int:
        return self.submitted - self.finished


def build_plan(fixtures: list[dict[str, Any]], *, count: int, via: str, only: list[str], channels: list[str], seed: int,
               recorded_only: bool) -> list[dict[str, Any]]:
    pool = [f for f in fixtures if (not only or f["fixture_id"] in only) and (not channels or f["channel"] in channels)]
    if recorded_only:
        pool = [f for f in pool if f.get("recording_seconds")]
    if not pool:
        raise SystemExit("no calls match: check --fixtures / --channels"
                         + (" (live audio needs the recordings made by tools\\gen_call_audio.py)" if recorded_only else ""))
    rng = random.Random(seed)
    plan: list[dict[str, Any]] = []
    while len(plan) < count:
        round_ = pool[:]
        rng.shuffle(round_)
        plan.extend({"fixture_id": f["fixture_id"], "channel": f["channel"], "via": via} for f in round_)
    return plan[:count]


async def request(http: httpx.AsyncClient, method: str, url: str, run: Run | None = None, **kw: Any) -> httpx.Response:
    delay = 0.5
    for attempt in range(4):
        try:
            response = await http.request(method, url, **kw)
        except httpx.TransportError as exc:
            if attempt == 3:
                raise
            if run is not None:
                run.errors[f"retry: {type(exc).__name__}"] += 1
        else:
            if response.status_code not in (429, 502, 503, 504) or attempt == 3:
                return response
        await asyncio.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def progress_line(run: Run, total: int) -> str:
    snap = run.latest
    elapsed = (time.monotonic() - run.began) if run.began else 0.0
    rate = snap.get("recent_calls_per_minute") or snap.get("calls_per_minute")
    extract = (snap.get("stages_ms", {}).get("extract") or {}).get("p50")
    return (f"[{clock(elapsed)}] sent {run.submitted}/{total}  done {run.completed}  queued {snap.get('queued', 0)}  "
            f"running {snap.get('in_flight', 0)}  failed {run.failed}  | {f'{rate:.0f}' if rate else '-'} calls/min  "
            f"ETA {clock(snap.get('eta_seconds'))}  | extract p50 {ms(extract)}  | "
            f"{tokens(snap.get('tokens_in', 0) + snap.get('tokens_out', 0))} tokens")


async def refresh(http: httpx.AsyncClient, run: Run) -> None:
    try:
        run.latest = (await http.get(f"{API}/metrics/pipeline")).json()
    except (httpx.HTTPError, ValueError):
        pass


async def poller(http: httpx.AsyncClient, run: Run, total: int, stop: asyncio.Event, interval: float, every: float) -> None:
    tty = sys.stdout.isatty()
    last_print = 0.0
    while not stop.is_set():
        await refresh(http, run)
        now = time.monotonic()
        if run.began and run.latest:
            if tty:
                sys.stdout.write(f"\r{CLEAR_LINE}{progress_line(run, total)}")
                sys.stdout.flush()
            elif now - last_print >= every:
                print(progress_line(run, total), flush=True)
                last_print = now
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def submit_all(http: httpx.AsyncClient, run: Run, *, concurrency: int, rate: float, asr_noise: bool) -> None:
    queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
    for index, item in enumerate(run.plan):
        queue.put_nowait((index, item))
    start = time.monotonic()

    async def worker() -> None:
        while True:
            try:
                index, item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if rate > 0:
                await asyncio.sleep(max(start + index / rate - time.monotonic(), 0))
            while run.window and run.outstanding() >= run.window:
                await asyncio.sleep(0.25)
            t0 = time.perf_counter()
            try:
                response = await request(http, "POST", f"{API}/ingest/simulate", run, json={**item, "asr_noise": asr_noise})
            except httpx.HTTPError as exc:
                run.submit_failures += 1
                run.errors[f"submit: {type(exc).__name__}"] += 1
                continue
            run.submit_ms.append((time.perf_counter() - t0) * 1000)
            if response.status_code != 200:
                run.submit_failures += 1
                run.errors[f"submit: HTTP {response.status_code} {response.text[:80]}"] += 1
                continue
            data = response.json()
            if data.get("duplicate_of"):
                run.duplicates += 1
            else:
                run.submitted += 1
                run.ids.append(data["conversation_id"])

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    run.submit_done = time.monotonic()


async def fetch_items(http: httpx.AsyncClient, wanted: set[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = (await http.get(f"{API}/conversations", params={"limit": 500, "offset": offset})).json()
        items += [i for i in page if i["conversation_id"] in wanted]
        if len(page) < 500:
            return items
        offset += 500


def is_clean(item: dict[str, Any]) -> bool:
    """No review reason and over the auto-approve bar (calls already auto-approved by policy count as clean)."""
    if item["status"] == "written":
        return True
    return item["status"] == "needs_review" and not item["review_reasons"] and bool(item["auto_approve_eligible"])


async def approve_clean(http: httpx.AsyncClient, items: list[dict[str, Any]], concurrency: int) -> tuple[int, int]:
    todo = [i["conversation_id"] for i in items if i["status"] == "needs_review" and is_clean(i)]
    gate = asyncio.Semaphore(concurrency)
    outcome: Counter = Counter()

    async def one(cid: str) -> None:
        async with gate:
            try:
                response = await request(http, "POST", f"{API}/conversations/{cid}/approve", json={"clicks": 1})
                outcome[response.status_code == 200 and response.json()["record"]["status"] == "written"] += 1
            except (httpx.HTTPError, ValueError, KeyError):
                outcome[False] += 1

    await asyncio.gather(*(one(cid) for cid in todo))
    return outcome[True], outcome[False]


def sizing(count: int, last: dict[str, Any]) -> list[str]:
    done = last.get("completed", 0)
    if done:
        per_call = (last.get("tokens_in", 0) + last.get("tokens_out", 0)) / done
        lines = [f"sizing        ~{tokens(count * per_call)} model tokens for {count} calls ({per_call:,.0f} per call, from the last batch)"]
        if last.get("cost_usd"):
            lines.append(f"              ~${count * last['cost_usd'] / done:,.2f} estimated (illustrative pricing table)")
        return lines
    return [f"sizing        ~{tokens(count * TOKENS_PER_HYBRID_CALL)} model tokens for {count} calls if the model runs "
            f"(hybrid, about {TOKENS_PER_HYBRID_CALL:,} per call)"]


def print_report(run: Run, args: argparse.Namespace, config: dict[str, Any], before: list[dict[str, Any]],
                 after: list[dict[str, Any]], approved: tuple[int, int] | None) -> dict[str, Any]:
    snap = run.latest
    wall = (run.finished_at or time.monotonic()) - (run.began or time.monotonic())
    modes = {k: v["effective"] for k, v in config["modes"].items()}
    rate = (run.finished / wall * 60) if wall > 0 and run.finished else None
    per_call = (snap.get("cost_usd") or 0) / max(snap.get("completed", 0), 1)
    latencies = [i["latency_seconds"] for i in after if i.get("latency_seconds") is not None]
    by_status = Counter(i["status"] for i in after)
    reasons = Counter(r for i in before for r in i["review_reasons"])
    clean = sum(is_clean(i) for i in before)
    print("\n" + "=" * 100)
    print(f"Bulk run: {run.submitted} calls via {args.via}   (modes: " + ", ".join(f"{k}={v}" for k, v in modes.items()) + ")")
    print("=" * 100)
    print(f"calls         {run.submitted} accepted, {run.finished} finished, {run.failed} failed"
          + (f", {run.duplicates} duplicates" if run.duplicates else "")
          + (f", {run.submit_failures} rejected at submit" if run.submit_failures else ""))
    print(f"wall time     {dur(wall)} from first submit to last call finished"
          + (f"   (all submitted after {dur(run.submit_done - run.began)})" if run.submit_done and run.began else ""))
    print(f"throughput    {f'{rate:.0f} calls/min ({rate / 60:.1f}/s)' if rate else '-'}   "
          f"with {snap.get('max_concurrency', '?')} calls processed in parallel")
    if run.submit_ms:
        print(f"ingest API    accepts a call in p50 {ms(pct(run.submit_ms, .5))}, p95 {ms(pct(run.submit_ms, .95))}, "
              f"max {ms(max(run.submit_ms))} (the work is queued, not done inline)")
    if latencies:
        wait = snap.get("queue_wait_seconds", {})
        print(f"latency       end to end p50 {dur(pct(latencies, .5))}, p95 {dur(pct(latencies, .95))}"
              f"   (includes a {dur(wait.get('p50'))} median wait in the queue behind the burst)")
    stages = {n: s for n, s in snap.get("stages_ms", {}).items() if n in ("transcribe", "policy", "extract") and s.get("count")}
    if stages and snap.get("submitted", 0) >= 0.9 * run.submitted:
        print("stages        " + "   ".join(f"{n} p50 {ms(s['p50'])} / p95 {ms(s['p95'])}" for n, s in stages.items()))
    elif stages:
        print("stages        (the pipeline went idle between arrivals, so the stage figures cover only the last burst)")
    print(f"model         {tokens(snap.get('tokens_in', 0))} tokens in, {tokens(snap.get('tokens_out', 0))} out; "
          f"engines {dict(snap.get('engines', {}))}; fallbacks to rules {snap.get('fallbacks', 0)}")
    print(f"est. cost     ${snap.get('cost_usd', 0):,.2f} total, ${per_call:.4f} per call"
          + (" (illustrative pricing table - validate before quoting)" if snap.get("cost_illustrative", True) else ""))
    if after:
        print("outcome       " + ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())))
        if reasons:
            print("needs a human " + ", ".join(f"{r} {n}" for r, n in reasons.most_common()))
        print(f"straight-through  {clean} of {len(before)} calls ({clean / max(len(before), 1):.0%}) have no review reason "
              "and clear the auto-approve bar")
    if approved:
        print(f"CRM writes    {approved[0]} calls approved and written, {approved[1]} failed")
    for item in snap.get("errors", []):
        print(f"error         {item['count']} x {item['error']}")
    for err, n in run.errors.most_common(5):
        print(f"client        {n} x {err}")
    if rate:
        print("projection    " + ", ".join(f"{t:,} calls: {dur(t / rate * 60)}" for t in (5000, 20000)) + " at this pace")
    print("=" * 100)
    return {"calls": run.submitted, "finished": run.finished, "failed": run.failed, "wall_seconds": round(wall, 1),
            "calls_per_minute": round(rate, 1) if rate else None, "snapshot": snap, "by_status": dict(by_status),
            "review_reasons": dict(reasons), "straight_through": clean, "modes": modes}


async def run_load(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_connections=args.concurrency + 8)
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(60.0, connect=5.0), limits=limits) as http:
        try:
            config = (await http.get(f"{API}/config")).json()
            fixtures = (await http.get(f"{API}/fixtures")).json()
            last = (await http.get(f"{API}/metrics/pipeline")).json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"cannot reach the VoiceCRM API at {base} ({type(exc).__name__}).\n"
                  "Start it with scripts\\run_backend.bat (or scripts\\run_bulk_backend.bat for the 800-call profile).")
            return 2
        modes = config["modes"]
        live = [k for k in ("transcription", "llm", "pii") if modes.get(k, {}).get("live")]
        only = [f.strip() for f in args.fixtures.split(",") if f.strip()]
        channels = [c.strip() for c in args.channels.split(",") if c.strip()]
        plan = build_plan(fixtures, count=args.count, via=args.via, only=only, channels=channels, seed=args.seed,
                          recorded_only=args.via == "audio" and bool(modes["transcription"]["live"]))
        parallel = max(last.get("max_concurrency", 8), 1)
        window = args.window if args.window is not None else (4 * parallel if args.via == "audio" else 0)
        mix = Counter(p["channel"] for p in plan)
        print(f"target        {base}   engine={config['engine']}   modes: "
              + ", ".join(f"{k}={v['effective']}" for k, v in modes.items()))
        print(f"plan          {len(plan)} calls via {args.via} from {len({p['fixture_id'] for p in plan})} scripted calls; "
              "channels " + ", ".join(f"{c} {n}" for c, n in sorted(mix.items())))
        print("pace          " + (f"{args.rate:g} calls/s" if args.rate else "one burst") + f"; {parallel} calls processed in parallel"
              + (f"; at most {window} outstanding" if window else ""))
        for line in sizing(len(plan), last):
            print(line)
        if last.get("active"):
            print(f"warning       the server is already working on {last.get('queued', 0) + last.get('in_flight', 0)} calls; "
                  "the figures will include them")
        if args.dry_run:
            print("\n--dry-run: nothing was sent.")
            return 0
        if live and not args.yes:
            print(f"\nLive Azure services are on ({', '.join(live)}): this run is billed. ", end="")
            if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Not started (pass --yes to confirm in a script).")
                return 2
        run = Run(plan=plan, window=window, base_completed=last.get("lifetime_completed", 0),
                  base_failed=last.get("lifetime_failed", 0))
        run.latest = last
        stop = asyncio.Event()
        watcher = asyncio.create_task(poller(http, run, len(plan), stop, args.poll, args.print_every))
        run.began = time.monotonic()
        print("\nstarting...\n")
        try:
            await submit_all(http, run, concurrency=args.concurrency, rate=args.rate, asr_noise=args.asr_noise)
            deadline = time.monotonic() + args.timeout
            while run.finished < run.submitted and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            run.finished_at = time.monotonic()
        finally:
            stop.set()
            await watcher
        await refresh(http, run)
        if sys.stdout.isatty():
            sys.stdout.write(f"\r{CLEAR_LINE}{progress_line(run, len(plan))}\n")
        timed_out = run.finished < run.submitted
        wanted = set(run.ids)
        before = await fetch_items(http, wanted)
        approved = await approve_clean(http, before, 16) if args.approve == "clean" else None
        after = await fetch_items(http, wanted) if approved else before
        report = print_report(run, args, config, before, after, approved)
        if timed_out:
            print(f"timed out after {args.timeout:.0f}s with {run.submitted - run.finished} calls unfinished (raise --timeout)")
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        return 1 if timed_out or run.failed or run.submit_failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Push a batch of calls through the running VoiceCRM API and report what it took.")
    p.add_argument("--base-url", default="http://127.0.0.1:8010", help="API root (default %(default)s)")
    p.add_argument("--count", type=int, default=800, help="calls to send (default %(default)s)")
    p.add_argument("--via", choices=["transcript", "audio"], default="transcript",
                   help="transcript = the platform's own transcript (Teams/Zoom/Genesys); audio = recordings through speech-to-text")
    p.add_argument("--rate", type=float, default=0, help="arrivals per second (default 0 = one burst)")
    p.add_argument("--concurrency", type=int, default=32, help="parallel submit requests (default %(default)s)")
    p.add_argument("--window", type=int, default=None,
                   help="max calls outstanding on the server (default: unlimited; 4x the parallelism for audio, which is large)")
    p.add_argument("--fixtures", default="", help="comma-separated scripted call ids (default: all)")
    p.add_argument("--channels", default="", help="comma-separated: teams,zoom,genesys,mobile,upload (default: all)")
    p.add_argument("--asr-noise", action="store_true", help="inject realistic speech-recognition mistakes")
    p.add_argument("--approve", choices=["none", "clean"], default="none",
                   help="clean = approve the calls that need no human, writing them to the CRM the server is configured with")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--poll", type=float, default=1.0, help="seconds between progress polls")
    p.add_argument("--print-every", type=float, default=5.0, help="seconds between progress lines when not on a terminal")
    p.add_argument("--timeout", type=float, default=3600, help="give up waiting after this many seconds")
    p.add_argument("--json", help="write the report to this file")
    p.add_argument("--yes", action="store_true", help="do not ask before spending on live Azure services")
    p.add_argument("--dry-run", action="store_true", help="print the plan and the sizing; send nothing")
    args = p.parse_args()
    if args.count < 1:
        p.error("--count must be at least 1")
    if sys.platform == "win32":
        os.system("")                    # switches the console to ANSI mode so the progress line can redraw in place
    return asyncio.run(run_load(args))


if __name__ == "__main__":
    raise SystemExit(main())

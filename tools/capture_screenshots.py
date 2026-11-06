r"""Regenerates the README gallery (docs/screenshots/*.png) from a throwaway VoiceCRM instance.

1. Starts the API on a free port with an empty temporary state directory and every service forced to mock/local,
   so nothing live is ever called and your own .state is left alone.
2. Runs two evaluations, then seeds a fixed synthetic demo set through the public API (the calls the UI makes).
3. Drives a headless Chrome / Edge (Playwright) over the built SPA and saves one PNG per view.

Prerequisites (tooling only, not project dependencies):
    .\.venv\Scripts\python.exe -m pip install playwright     # uses the installed Chrome or Edge, no browser download
    cd frontend && npm run build                              # the API serves frontend\dist

Run from the repo root:
    .\.venv\Scripts\python.exe tools\capture_screenshots.py [--out docs\screenshots] [--only inbox,review]
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WIDTH = 1440                 # desktop viewport the UI is designed for
SIDEBAR, TOPBAR = 240, 57    # px: width of the left navigation, height of the service-chip bar

# Oldest first: the inbox lists newest first, so the last items seeded are the ones on top of it.
# action: approve | pending | learn:<product id> (a reviewer picks that product for the unknown fund name, then approves)
SEED: list[tuple[str, str, str]] = [
    ("legacy-1", "pb-teams-legacy-blue-chip", "learn:SYN-000002"),
    ("legacy-2", "retail-zoom-legacy-heritage-compass", "learn:SYN-000021"),
    ("alias", "pb-teams-legacy-blue-chip", "pending"),            # same call again: resolved by the learned alias
    ("eu-zoom", "intl-eu-zoom-euro-bond-ucits", "approve"),       # EU market: redacted transcript kept in the vault
    ("genesys-service", "pb-genesys-inbound-service", "approve"),
    ("genesys-retail", "retail-genesys-class-conversion", "approve"),
    ("zoom-retail", "retail-zoom-target-date-retirement", "approve"),
    ("teams-etf", "pb-teams-etf-vs-mutual-fund", "approve"),
    ("privacy", "retail-teams-relationship-privacy", "approve"),
    ("zoom", "pb-zoom-equity-income-advisor", "pending"),
    ("spanish", "pb-teams-spanish-clase-c", "pending"),
    ("mobile", "pb-mobile-in-person-multi-product", "pending"),
    ("pick", "pb-ambiguous-equity-fund", "pending"),              # class never said: top-3 pick list
    ("hero", "pb-teams-equity-class-c", "pending"),
]

# <main> is the app's scroll container and the review sidebar is sticky with its own scroll: lay both out in full
UNSTICK = "main aside{position:static!important;max-height:none!important;overflow:visible!important}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _remove_tree(path: Path) -> None:
    """Windows keeps files locked for a moment after a process is killed, so retry before giving up."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.5)
    print(f"warning: could not remove {path}", file=sys.stderr)


@contextmanager
def throwaway_server() -> Iterator[str]:
    """Runs the API against an empty temp state dir with every service on its mock/local adapter; yields the base URL."""
    state = Path(tempfile.mkdtemp(prefix="voicecrm-shots-"))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICECRM_")}
    env.update(
        VOICECRM_STATE_DIR=str(state), VOICECRM_DEMO_MODE="true", VOICECRM_TRANSCRIPTION_MODE="mock",
        VOICECRM_LLM_MODE="rules", VOICECRM_SEARCH_MODE="local", VOICECRM_PII_MODE="local",
        VOICECRM_CRM_MODE="local", VOICECRM_VAULT_MODE="local",
    )
    log_path = state / "server.log"
    log = log_path.open("w")
    proc = subprocess.Popen([sys.executable, "-m", "voicecrm.cli", "serve", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"the API exited during start-up:\n{log_path.read_text(errors='replace')[-1500:]}")
            try:
                if httpx.get(f"{base}/api/v1/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("the API did not become healthy within 60 s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        _remove_tree(state)


def _run_call(api: httpx.Client, fixture: str) -> tuple[str, dict]:
    cid = api.post("/ingest/simulate", json={"fixture_id": fixture}).raise_for_status().json()["conversation_id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        detail = api.get(f"/conversations/{cid}").json()
        if detail["record"]["status"] not in ("received", "processing"):
            return cid, detail
        time.sleep(0.3)
    raise TimeoutError(f"{fixture} did not finish processing")


def _approve(api: httpx.Client, cid: str, detail: dict, pick: str | None, clicks: int) -> None:
    picks: dict[str, str] = {}
    for mention in detail["record"]["proposal"]["result"]["products"]:
        if mention.get("chosen_product_id") is None:
            product = pick or (mention["candidates"][0]["product_id"] if mention.get("candidates") else None)
            if product:
                picks[mention["mention_id"]] = product
    api.post(f"/conversations/{cid}/approve", json={"product_choices": picks, "clicks": clicks}).raise_for_status()


def seed(base: str) -> dict[str, str]:
    api = httpx.Client(base_url=f"{base}/api/v1", timeout=180)
    # evaluations first: the baseline must predate every learned alias
    for body in ({"label": "Baseline"}, {"apply_learning": True, "label": "With learning"}):
        api.post("/eval/run", json=body).raise_for_status()
    ids: dict[str, str] = {}
    for key, fixture, action in SEED:
        cid, detail = _run_call(api, fixture)
        if action == "approve":
            _approve(api, cid, detail, None, clicks=1)
        elif action.startswith("learn:"):
            _approve(api, cid, detail, action.split(":", 1)[1], clicks=3)
        ids[key] = cid
        print(f"  seeded {fixture:<38} {action}")
    return ids


@dataclass
class Env:
    base: str
    ids: dict[str, str]
    out: Path
    browser: Browser
    page: Page


SHOTS: dict[str, Callable[[Env], None]] = {}


def shot(name: str) -> Callable[[Callable[[Env], None]], Callable[[Env], None]]:
    def register(fn: Callable[[Env], None]) -> Callable[[Env], None]:
        SHOTS[name] = fn
        return fn
    return register


def open_view(env: Env, path: str, ready: str) -> None:
    env.page.set_viewport_size({"width": WIDTH, "height": 900})
    env.page.goto(env.base + path, wait_until="networkidle")
    env.page.wait_for_selector(ready)
    env.page.add_style_tag(content=UNSTICK)
    env.page.wait_for_timeout(600)


def fit(env: Env) -> None:
    """Grows the viewport until <main> shows all of its content, so nothing hides behind internal scrolling."""
    for _ in range(3):
        height = env.page.evaluate("document.getElementById('main').scrollHeight") + TOPBAR
        env.page.set_viewport_size({"width": WIDTH, "height": height})
        env.page.wait_for_timeout(250)


def rect(env: Env, selector: str, nth: int = 0) -> dict[str, float]:
    return env.page.evaluate(
        "([sel, i]) => { const r = document.querySelectorAll(sel)[i].getBoundingClientRect();"
        " return {x: r.x, y: r.y, right: r.right, bottom: r.bottom}; }", [selector, nth])


def card(card_id: str) -> str:
    return f'section[aria-labelledby="{card_id}"]'


def save_main(env: Env, name: str, bottom: float) -> None:
    """Saves the page without the left navigation, from the top bar down to `bottom` (page px)."""
    env.page.screenshot(path=env.out / f"{name}.png",
                        clip={"x": SIDEBAR, "y": 0, "width": WIDTH - SIDEBAR, "height": math.ceil(bottom)})


def save_cards(env: Env, name: str, selectors: list[str], pad: int = 6) -> None:
    rects = [rect(env, s) for s in selectors]
    x0, y0 = min(r["x"] for r in rects) - pad, min(r["y"] for r in rects) - pad
    x1, y1 = max(r["right"] for r in rects) + pad, max(r["bottom"] for r in rects) + pad
    env.page.screenshot(path=env.out / f"{name}.png", clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})


@shot("inbox")
def inbox(env: Env) -> None:
    open_view(env, "/", "#main li.card")
    bottom = rect(env, "#main li.card", 4)["bottom"] + 12              # the first five conversations
    env.page.set_viewport_size({"width": WIDTH, "height": math.ceil(bottom) + 20})
    env.page.wait_for_timeout(300)
    save_main(env, "inbox", bottom)


@shot("review")
def review(env: Env) -> None:
    open_view(env, f"/conversations/{env.ids['hero']}", "#tasks-title")
    fit(env)
    save_main(env, "review", rect(env, card("note-title"))["bottom"] + 16)


@shot("review-structured")
def review_structured(env: Env) -> None:
    open_view(env, f"/conversations/{env.ids['hero']}", "#tasks-title")
    fit(env)
    save_cards(env, "review-structured", [card("products-title"), card("topics-title"), card("tasks-title")])


@shot("pick")
def pick(env: Env) -> None:
    open_view(env, f"/conversations/{env.ids['pick']}", "#products-title")
    fit(env)
    save_cards(env, "pick", [card("products-title")])


@shot("privacy")
def privacy(env: Env) -> None:
    open_view(env, f"/conversations/{env.ids['privacy']}", "#privacy-title")
    fit(env)
    save_cards(env, "privacy", [card("privacy-title")])


@shot("sources")
def sources(env: Env) -> None:
    open_view(env, "/sources", "#channels-title")
    fit(env)
    save_cards(env, "sources", [card("channels-title")])


@shot("capture")
def capture(env: Env) -> None:
    ctx = env.browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  is_mobile=True, has_touch=True)
    try:
        page = ctx.new_page()
        page.goto(env.base + "/capture", wait_until="networkidle")
        page.wait_for_selector("text=Choose an audio file instead")
        page.wait_for_timeout(600)
        page.screenshot(path=env.out / "capture.png", clip={"x": 0, "y": 0, "width": 390, "height": 800})
    finally:
        ctx.close()


@shot("policies")
def policies(env: Env) -> None:
    open_view(env, "/policies", "#processing-title")
    fit(env)
    bottom = max(rect(env, card("notes-policy-title"))["bottom"], rect(env, card("processing-title"))["bottom"])
    save_main(env, "policies", bottom + 16)


@shot("crm")
def crm(env: Env) -> None:
    open_view(env, "/crm", "role=tab")
    env.page.get_by_role("tab", name="Insights").click()
    env.page.wait_for_selector("#main [role=tabpanel] ul.grid > li")
    fit(env)
    # the newest insights come first: keep to the cards that show no local vault path (see SEED order)
    save_main(env, "crm", rect(env, "#main [role=tabpanel] ul.grid > li", 3)["bottom"] + 12)   # two rows of insights


@shot("scoreboard")
def scoreboard(env: Env) -> None:
    open_view(env, "/scoreboard", "#compare-title")
    fit(env)
    save_main(env, "scoreboard", rect(env, 'section[aria-label="Metrics for the selected run"]')["bottom"] + 16)


@shot("live-audio")
def live_audio(env: Env) -> None:
    """A recorded call on the Sources page: the real speech recording, playable before it is processed."""
    open_view(env, "/sources", "#simulate-title")
    cards = env.page.locator("#main li:has(audio)")
    if cards.count() == 0:
        print("    (skipped: no recordings in data\\audio\\live; run scripts\\make_audio.bat first)")
        return
    fit(env)
    cards.first.screenshot(path=env.out / "live-audio.png")


@shot("load")
def load(env: Env) -> None:
    """The Inbox after a burst of 200 calls through the real ingest path: live throughput, stages, queue and cost.

    Runs last because it adds 200 conversations. The adapters are mock, so the figures show the instrumentation, not the
    speed of the live Azure services (those are measured in docs/live-demo.md).
    """
    api = httpx.Client(base_url=f"{env.base}/api/v1", timeout=60)
    fixtures = [f["fixture_id"] for f in api.get("/fixtures").json()]
    for index in range(200):
        api.post("/ingest/simulate", json={"fixture_id": fixtures[index % len(fixtures)]}).raise_for_status()
    deadline = time.time() + 180
    while time.time() < deadline:
        stats = api.get("/metrics/pipeline").json()
        if not stats["active"] and stats["completed"] + stats["failed"] >= 200:
            break
        time.sleep(0.5)
    open_view(env, "/", "section[aria-labelledby='load-title']")
    env.page.wait_for_timeout(1500)
    save_cards(env, "load", [card("load-title")])


def launch(pw) -> Browser:
    for channel in ("chrome", "msedge", None):
        try:
            return pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
        except Exception:
            continue
    raise RuntimeError("no Chrome, Edge or Playwright Chromium found (run: python -m playwright install chromium)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--only", default="", help=f"comma-separated subset of: {', '.join(SHOTS)}")
    args = parser.parse_args()
    wanted = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in SHOTS]
    if unknown:
        parser.error(f"unknown view(s): {', '.join(unknown)}")
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        parser.error("frontend\\dist is missing: run `npm run build` in frontend first")

    args.out.mkdir(parents=True, exist_ok=True)
    with throwaway_server() as base:
        print(f"throwaway API on {base}; seeding ...")
        ids = seed(base)
        with sync_playwright() as pw:
            browser = launch(pw)
            try:
                env = Env(base, ids, args.out, browser, browser.new_page(viewport={"width": WIDTH, "height": 900}))
                for name, fn in SHOTS.items():
                    if wanted and name not in wanted:
                        continue
                    fn(env)
                    path = args.out / f"{name}.png"
                    print(f"  {path.name:<24} {path.stat().st_size // 1024:>4} KB")
            finally:
                browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

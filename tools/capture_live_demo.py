"""Captures the Live demo page against a running API with real Azure services: the audio run and the bulk-run report.

    .venv\\Scripts\\python.exe tools\\capture_live_demo.py --base-url http://127.0.0.1:8010 --calls 60
    .venv\\Scripts\\python.exe tools\\capture_live_demo.py --yes                  skip the spending prompt

It drives the page the way a presenter does: press "Process with Azure" on the hero recording and approve it, then start a
bulk run, confirm the billed-services dialog, wait for the report, and finally remove the load-test calls. Writes
docs\\screenshots\\live-demo-audio.png and live-demo-bulk.png. It spends real money (a few cents for the audio call, about
$0.004 per transcript call) and writes the hero call to the CRM, so point it at a scratch server: scripts\\run_bulk_backend.bat
keeps its own state folder. Needs the recordings from scripts\\make_audio.bat and Playwright (python -m playwright install chromium).
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WIDTH = 1440
TOPBAR = 57                  # px: height of the service-chip bar
HERO = "pb-teams-equity-class-c"


def launch(pw) -> Browser:
    for channel in ("chrome", "msedge", None):
        try:
            return pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
        except Exception:
            continue
    raise RuntimeError("no Chrome, Edge or Playwright Chromium found (run: python -m playwright install chromium)")


def fit(page: Page) -> None:
    """Grows the viewport until <main> shows all of its content, so nothing hides behind internal scrolling."""
    for _ in range(3):
        height = page.evaluate("document.getElementById('main').scrollHeight") + TOPBAR
        page.set_viewport_size({"width": WIDTH, "height": min(height, 7000)})
        page.wait_for_timeout(250)


def rect(page: Page, selector: str) -> dict[str, float]:
    return page.evaluate(
        "(sel) => { const r = document.querySelector(sel).getBoundingClientRect();"
        " return {x: r.x, y: r.y, right: r.right, bottom: r.bottom}; }", selector)


def save_cards(page: Page, path: Path, selectors: list[str], pad: int = 6) -> None:
    rects = [rect(page, s) for s in selectors]
    x0, y0 = min(r["x"] for r in rects) - pad, min(r["y"] for r in rects) - pad
    x1, y1 = max(r["right"] for r in rects) + pad, max(r["bottom"] for r in rects) + pad
    page.screenshot(path=path, clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    print(f"  saved {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--calls", type=int, default=60, help="calls in the bulk run (default 60)")
    parser.add_argument("--yes", action="store_true", help="do not ask before spending")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        config = httpx.get(f"{base}/api/v1/config", timeout=10).json()
        fixtures = httpx.get(f"{base}/api/v1/fixtures", timeout=10).json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"cannot reach the VoiceCRM API at {base} ({type(exc).__name__}): start it with scripts\\run_bulk_backend.bat")
        return 2
    modes = config["modes"]
    if not all(modes[k]["live"] for k in ("transcription", "pii", "llm")):
        print("Speech, Language and the model must all be live for these screenshots (see the chips in the top bar).")
        return 2
    if not any(f["fixture_id"] == HERO and f.get("recording_seconds") for f in fixtures):
        print("no recording of the hero call: run scripts\\make_audio.bat first")
        return 2
    if httpx.get(f"{base}/api/v1/live/load", timeout=10).json()["state"] in ("preparing", "running", "stopping"):
        print("a load test is already running on that server")
        return 2
    if modes["crm"]["live"]:
        print("the CRM is live: this tool approves a call and would write it there. Use the local CRM simulator.")
        return 2
    if not args.yes and input(f"This spends real money (about ${0.03 + 0.0044 * args.calls:.2f}) on {base}. Continue? [y/N] ").lower() != "y":
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch(pw)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": 900})
            page.goto(f"{base}/live", wait_until="networkidle")
            page.get_by_role("heading", name="Live demo").first.wait_for()

            print("A: the hero recording through Azure Speech, Language and the model")
            page.locator("label:has(input[name=audio-call])", has_text="Class C fees").first.click()
            page.get_by_role("button", name="Process with Azure").click()
            page.get_by_text("Approve and write to the CRM").wait_for(timeout=150_000)
            page.get_by_role("button", name="Approve and write to the CRM").click()
            page.get_by_text("Written to the CRM:").wait_for(timeout=30_000)
            page.wait_for_timeout(600)
            fit(page)
            save_cards(page, args.out / "live-demo-audio.png", ['section[aria-labelledby^="run-"]'])

            print(f"C: {args.calls} calls at once")
            page.get_by_label("Number of calls").fill(str(args.calls))
            page.get_by_text("Billed:").wait_for(timeout=30_000)
            page.wait_for_timeout(900)
            page.get_by_role("button", name=f"Start {args.calls}-call run").click()
            page.get_by_role("dialog").get_by_role("button", name=f"Start {args.calls} calls").click()
            page.get_by_text("Run complete").first.wait_for(timeout=math.ceil(args.calls * 4 + 120) * 1000)
            page.wait_for_timeout(1200)
            fit(page)
            save_cards(page, args.out / "live-demo-bulk.png", ['section[aria-labelledby="load-report-title"]', 'section[aria-labelledby="load-title"]'])

            page.get_by_role("button", name="Remove load-test calls").click()
            page.get_by_role("dialog").get_by_role("button", name="Remove them").click()
            page.wait_for_timeout(1500)
            print("  removed the load-test calls again")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

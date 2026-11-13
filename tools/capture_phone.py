r"""Captures the phone-line screenshots (docs/screenshots/phone-*.png) from a real call.

Needs the phone line set up and running (docs/phone-line.md: the app, the tunnel and the Event Grid subscriptions),
VOICECRM_PHONE_SELLER left empty (the seller answers in the browser console), a built frontend (npm run build), Azure text to speech
(for the two voices), and Playwright with Chrome or Edge:

    .venv\Scripts\python.exe tools\capture_phone.py --base-url http://localhost:8010

It speaks data/phone/phone-call.json with tools/gen_meeting_audio.py --stems, then runs two browsers: one dials from the dial pad,
the other answers in the seller console, and each plays its own voice as the microphone. The call is about a minute and a half and
leaves one conversation in the Inbox (it is a real call: it is recorded, transcribed and billed like any other). Every tunnel
address on screen is replaced before a screenshot is taken.

Writes: phone-ready.png, phone-ringing.png, phone-in-call.png, phone-dialer.png, phone-inbox.png, phone-review.png.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
SCRIPT = REPO / "data" / "phone" / "phone-call.json"
CALLER_LEAD_SECONDS = 28          # the caller speaks about when the seller joins (the notice and the ring come first)
HANG_UP_AFTER_ACCEPT = 50         # both voices are about 44 s long
SANITIZE = r"""() => {
  const pattern = /[a-z0-9]+-(\d+)\.([a-z0-9]+)\.devtunnels\.ms/g;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (/devtunnels\.ms/.test(node.nodeValue)) node.nodeValue = node.nodeValue.replace(pattern, 'example-$1.$2.devtunnels.ms');
  }
}"""


class Env:
    def __init__(self, base: str, out: Path) -> None:
        self.base, self.out = base.rstrip("/"), out

    def api(self, path: str):
        with urllib.request.urlopen(f"{self.base}/api/v1{path}", timeout=30) as response:
            return json.loads(response.read() or "null")

    def status(self) -> dict:
        return self.api("/sources/phone")

    def shot(self, target, name: str, *, full_page: bool = False) -> None:
        """A screenshot of a page or an element, after every tunnel address on screen has been replaced."""
        page = target if hasattr(target, "goto") else target.page
        page.evaluate(SANITIZE)
        path = self.out / f"{name}.png"
        target.screenshot(path=str(path), **({"full_page": True} if full_page else {}))
        print(f"  {path.relative_to(REPO)}")


def log(message: str) -> None:
    print(message, flush=True)


def make_voices(folder: Path) -> tuple[Path, Path]:
    """The two microphones: the customer's voice (after a pause for the notice and the ring) and the seller's."""
    subprocess.run([sys.executable, str(TOOLS / "gen_meeting_audio.py"), str(SCRIPT), "--stems", "--force", "--rate", "16000",
                    "--lead-in", "1", "--out", str(folder)], check=True, capture_output=True)

    def read(path: Path):
        with wave.open(str(path), "rb") as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())

    channels, width, rate, customer = read(folder / "phone-call.customer.wav")
    _, _, _, seller = read(folder / "phone-call.seller.wav")
    caller = folder / "caller-mic.wav"
    with wave.open(str(caller), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(int(rate * CALLER_LEAD_SECONDS) * width * channels) + customer)
    return caller, folder / "phone-call.seller.wav"


def launch(pw: Playwright, wav: Path) -> Browser:
    args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav.as_posix()}%noloop"]
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=channel, args=args)
        except Exception:                                          # noqa: BLE001 (try the next browser)
            continue
    raise RuntimeError("no Chrome or Edge found (run: python -m playwright install chrome)")


def new_page(browser: Browser) -> Page:
    return browser.new_context(viewport={"width": 1360, "height": 1000}, permissions=["microphone"]).new_page()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8010", help="the running app (use localhost: the Calling SDK refuses 127.0.0.1)")
    parser.add_argument("--out", type=Path, default=REPO / "docs" / "screenshots")
    args = parser.parse_args()
    if "127.0.0.1" in args.base_url:
        print("Use http://localhost:<port>: the Azure Calling SDK does not run on 127.0.0.1.")
        return 2
    env = Env(args.base_url, args.out)
    status = env.status()
    if status["mode"] == "off" or not status["configured"]:
        print(f"The phone line is not ready: {status['problem']}")
        return 2
    if status["seller"]["mode"] == "teams":
        print("VOICECRM_PHONE_SELLER is a Teams user: leave it empty for this capture, so the console takes the call.")
        return 2
    before = {c["call_id"] for c in [*status["active"], *status["recent"]]}
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="voicecrm-phone-") as tmp, sync_playwright() as pw:
        log("Speaking the call (Azure text to speech)...")
        caller_wav, seller_wav = make_voices(Path(tmp))
        seller_browser, caller_browser = launch(pw, seller_wav), launch(pw, caller_wav)
        try:
            seller, caller = new_page(seller_browser), new_page(caller_browser)
            seller.goto(f"{env.base}/live", wait_until="networkidle")
            seller.locator("#scenario-d").scroll_into_view_if_needed()
            seller.get_by_role("switch", name="Answer calls in this browser").click()
            seller.get_by_text("Waiting for calls").wait_for(timeout=90_000)
            deadline = time.time() + 20
            while time.time() < deadline and not env.status()["seller"]["online"]:
                time.sleep(0.5)

            log("Is the phone line ready?")
            seller.get_by_role("button", name="Check phone setup").click()
            seller.get_by_text("checked in").wait_for(timeout=120_000)
            env.shot(seller.locator("[aria-labelledby='phone-setup-title']"), "phone-ready")

            caller.goto(f"{env.base}/live", wait_until="networkidle")
            caller.locator("#dialer-name").scroll_into_view_if_needed()
            for field, value in (("name", "Dana Whitfield"), ("org", "Harborview Pension Fund"),
                                 ("email", "dana.whitfield@harborview-pension.example"), ("phone", "+1 206 555 0111")):
                caller.fill(f"#dialer-{field}", value)
            caller.get_by_role("button", name="Call", exact=True).click()
            log("Dialling...")

            seller.get_by_role("button", name="Accept").wait_for(timeout=120_000)
            env.shot(seller.locator("[aria-labelledby='console-title']"), "phone-ringing")
            seller.get_by_role("button", name="Accept").click()
            accepted = time.time()
            time.sleep(14)
            env.shot(seller.locator("[aria-labelledby='phone-live-title']"), "phone-in-call")
            env.shot(caller.locator("[aria-labelledby='dialer-title']"), "phone-dialer")
            while time.time() - accepted < HANG_UP_AFTER_ACCEPT:
                time.sleep(1)
            caller.get_by_role("button", name="Hang up").click()
            log("Hung up; waiting for the recording...")

            call = None
            end = time.time() + 300
            while time.time() < end:
                s = env.status()
                call = next((c for c in [*s["active"], *s["recent"]] if c["call_id"] not in before), None)
                if call and call["state"] in ("ingested", "failed", "missed"):
                    break
                time.sleep(2)
            if not call or call["state"] != "ingested" or not call["conversation_id"]:
                print(f"The call did not become a conversation: {call and call['state']} {call and call['error']}")
                return 1
            conversation = call["conversation_id"]
            for _ in range(90):
                if env.api(f"/conversations/{conversation}")["record"]["status"] != "processing":
                    break
                time.sleep(2)

            seller.goto(f"{env.base}/", wait_until="networkidle")
            env.shot(seller, "phone-inbox")
            seller.set_viewport_size({"width": 1360, "height": 2400})            # tall, so the page scrolls inside nothing
            seller.goto(f"{env.base}/conversations/{conversation}", wait_until="networkidle")
            env.shot(seller, "phone-review", full_page=True)
        finally:
            caller_browser.close()
            seller_browser.close()
    log("Done: the call is in the Inbox.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

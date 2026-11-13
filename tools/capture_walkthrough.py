r"""Regenerates the walkthrough screenshots (docs/screenshots/walkthrough-*.png) that docs/walkthrough.md shows.

Every image is a LIVE capture. The tool starts a throwaway server with an empty temporary state (your own .state is never
touched), runs it on the real Azure services configured in .env (Azure Speech, Azure AI Language and the Foundry model) and
forces Teams ingestion off, so nothing from your tenant can end up in an image. The CRM, the vault and the catalogue search are
the local ones the MVP ships with, so their chips read "Mock / local".

    .\.venv\Scripts\python.exe tools\capture_walkthrough.py [--only services,a,policies] [--seconds N] [--out docs\screenshots]

Needs the recordings from scripts\make_audio.bat (data\audio\live), `npm run build`, and Playwright with Chrome or Edge.
A full run is about 10 minutes and under $0.60 of Azure usage; most of the time is the real-time recording of the `record`
part (--seconds shortens it). Before it saves an image it scans the visible text with your .deny-terms.local rules and a few
generic patterns (profile paths, tenant and resource host names) and stops if anything matches.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import math
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Locator, Playwright, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_screenshots as cs  # noqa: E402  (the throwaway server and the layout helpers)
import check_public as cp  # noqa: E402  (the deny-list rules)

HERO = "pb-teams-equity-class-c"
HERO_WAV = cs.ROOT / "data" / "audio" / "live" / f"{HERO}.wav"
HERO_SECONDS = 190                       # the recording is 187 s long
READY = re.compile(r"Approve and write to the CRM|A person has to look")
RUN = 'section[aria-labelledby^="run-"]'
ASIDE = 'aside[aria-label="Pipeline, privacy and retention"]'
PIPELINE = 'section.card:has(h2:text-is("Pipeline"))'
NO_RECORDINGS = "    (skipped: no recordings in data\\audio\\live; run scripts\\make_audio.bat first)"
PRIVACY_CALL = "retail-teams-relationship-privacy"
# One call per channel, written to the CRM with one click: what the Inbox and the CRM page show as done.
CRM_CALLS = (("crm-teams", "pb-teams-etf-vs-mutual-fund"), ("crm-zoom", "pb-zoom-equity-income-advisor"), ("crm-genesys", "pb-genesys-inbound-service"))

GUARDS = [
    ("a user profile path", re.compile(r"[A-Za-z]:\\Users\\|/Users/\w+/", re.IGNORECASE)),
    ("an Azure resource host name", re.compile(r"(?<![\w-])(?!your-resource)[a-z0-9][a-z0-9-]*\.(?:cognitiveservices|services\.ai|openai)\.azure\.com\b", re.IGNORECASE)),
    ("a tenant host name", re.compile(r"\.onmicrosoft\.com\b", re.IGNORECASE)),
]
DENY = cp.load_rules(cp.DENY_FILE.read_text(encoding="utf-8")) if cp.DENY_FILE.exists() else []

# The services check lists the Azure resource host names; they become placeholders before anything is saved.
HIDE_HOSTS = r"""
() => {
  const rx = /\b[a-z0-9][a-z0-9-]*\.(cognitiveservices|services\.ai|openai)\.(azure\.com)\b/gi;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue.replace(rx, 'your-resource.$1.$2');
    if (text !== node.nodeValue) node.nodeValue = text;
  }
}
"""

# Shows only some stage rows of the Pipeline card (and optionally its usage-and-cost footer); an empty list shows all.
PIPELINE_ROWS = r"""
([keep, footer]) => {
  const card = [...document.querySelectorAll('section.card')].find((s) => s.querySelector('h2')?.textContent.trim() === 'Pipeline');
  [...card.querySelectorAll('ol > li')].forEach((li, i) => { li.style.display = keep.length === 0 || keep.includes(i) ? '' : 'none'; });
  const foot = card.querySelector('div.border-t.bg-neutral-50');
  if (foot) foot.style.display = keep.length === 0 || footer ? '' : 'none';
}
"""

OPEN_DETAILS = r"""
([scope, label]) => {
  document.querySelectorAll(scope + ' details').forEach((d) => {
    if ((d.querySelector('summary')?.textContent ?? '').trim().startsWith(label)) d.open = true;
  });
}
"""

SCROLL_TRANSCRIPT = r"""
(id) => {
  const list = document.querySelector('ol[aria-label="Transcript segments"]');
  const item = [...list.children].find((li) => li.textContent.includes('#' + id));
  const from = item.previousElementSibling ?? item;
  list.scrollTop = Math.max(0, from.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop);
}
"""

PARTS: dict[str, Callable[[cs.Env], None]] = {}


def part(name: str) -> Callable[[Callable[[cs.Env], None]], Callable[[cs.Env], None]]:
    def register(fn: Callable[[cs.Env], None]) -> Callable[[cs.Env], None]:
        PARTS[name] = fn
        return fn
    return register


# ---- saving ------------------------------------------------------------------------------------------------------
def log(message: str) -> None:
    print(f"  {message}", flush=True)


def guard(env: cs.Env) -> None:
    """Stops before an image is saved when the page shows something that must not be published."""
    text = env.page.evaluate("document.body.innerText")
    hits = {label for label, rx in GUARDS if rx.search(text)} | {rule.label for rule in DENY if rule.pattern.search(text)}
    if hits:
        raise RuntimeError(f"not saving an image of {env.page.url}: the page shows {', '.join(sorted(hits))}")


def save_clip(env: cs.Env, name: str, x: float, y: float, width: float, height: float) -> None:
    guard(env)
    path = env.out / f"walkthrough-{name}.png"
    env.page.screenshot(path=path, clip={"x": max(0.0, x), "y": max(0.0, y), "width": width, "height": height})
    log(f"{path.name:<42} {path.stat().st_size // 1024:>4} KB")


def box(env: cs.Env, target: Locator | str) -> dict[str, float]:
    locator = target if isinstance(target, Locator) else env.page.locator(target).first
    found = locator.bounding_box()
    if found is None:
        raise RuntimeError(f"{target} is not on the page")
    return found


def save_boxes(env: cs.Env, name: str, targets: list[Locator | str], pad: int = 6) -> None:
    """The smallest rectangle around the given elements, with a little padding."""
    boxes = [box(env, target) for target in targets]
    x0, y0 = min(b["x"] for b in boxes) - pad, min(b["y"] for b in boxes) - pad
    x1, y1 = max(b["x"] + b["width"] for b in boxes) + pad, max(b["y"] + b["height"] for b in boxes) + pad
    save_clip(env, name, x0, y0, x1 - x0, y1 - y0)


def save_top(env: cs.Env, name: str, bottom: float) -> None:
    """The page without the left navigation, from the top bar (the service chips) down to `bottom`."""
    save_clip(env, name, cs.SIDEBAR, 0, cs.WIDTH - cs.SIDEBAR, math.ceil(bottom))


def save_page(env: cs.Env, name: str) -> None:
    save_top(env, name, env.page.viewport_size["height"])


# ---- driving the app -----------------------------------------------------------------------------------------------
def http(env: cs.Env) -> httpx.Client:
    return httpx.Client(base_url=f"{env.base}/api/v1", timeout=180)


def wait_processed(api: httpx.Client, cid: str, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = api.get(f"/conversations/{cid}").json()
        if detail["record"]["status"] not in ("received", "processing"):
            return detail
        time.sleep(0.5)
    raise TimeoutError(f"conversation {cid} did not finish processing")


def run_call(env: cs.Env, key: str, fixture: str, approve: bool = False, **extra: object) -> str:
    """Runs a scripted call through the live pipeline once per key; `approve` sends it to the local CRM like one click would."""
    if key not in env.ids:
        api = http(env)
        body = {"fixture_id": fixture, **extra}
        cid = api.post("/ingest/simulate", json=body).raise_for_status().json()["conversation_id"]
        detail = wait_processed(api, cid)
        if detail["record"]["status"] == "failed":
            raise RuntimeError(f"{fixture} failed: {detail['record'].get('error')}")
        if approve:
            api.post(f"/conversations/{cid}/approve", json={}).raise_for_status()
        env.ids[key] = cid
        log(f"ran {fixture} ({key}): {detail['record']['status']}")
    return env.ids[key]


def open_review(env: cs.Env, cid: str) -> None:
    cs.open_view(env, f"/conversations/{cid}", "#tasks-title")
    cs.fit(env)


def pipeline_rows(env: cs.Env, name: str, rows: list[int], footer: bool = False) -> None:
    """The Pipeline card reduced to some of its stages, saved as a card of its own."""
    env.page.evaluate(PIPELINE_ROWS, [rows, footer])
    cs.fit(env)
    save_boxes(env, name, [PIPELINE])
    env.page.evaluate(PIPELINE_ROWS, [[], False])
    cs.fit(env)


def open_details(env: cs.Env, scope: str, label: str) -> None:
    env.page.evaluate(OPEN_DETAILS, [scope, label])
    cs.fit(env)


def aside_card(env: cs.Env, index: int) -> Locator:
    return env.page.locator(f"{ASIDE} > *").nth(index)


def wait_ready(env: cs.Env, scope: str) -> Locator:
    run = env.page.locator(f"{scope} {RUN}")
    run.get_by_text(READY).first.wait_for(timeout=120_000)
    cs.fit(env)
    return run


def has_recordings(env: cs.Env) -> bool:
    return env.page.locator("#scenario-a audio").count() > 0


# ---- part 1: start it live -----------------------------------------------------------------------------------------
@part("services")
def services(env: cs.Env) -> None:
    """Live demo: which services are live, then what 'Check services now' proves about each of them."""
    page = env.page
    cs.open_view(env, "/live", "#services-title")
    cs.fit(env)
    save_top(env, "services", box(env, cs.card("services-title"))["y"] + box(env, cs.card("services-title"))["height"] + 16)
    page.get_by_role("button", name="Check services now").click()
    page.get_by_text(re.compile(r"checked in \d")).first.wait_for(timeout=120_000)
    page.evaluate(HIDE_HOSTS)
    cs.fit(env)
    card = box(env, cs.card("services-title"))
    heading = box(env, page.locator(cs.card("services-title")).get_by_text(re.compile("scenario readiness", re.IGNORECASE)).first)
    save_clip(env, "services-check", card["x"] - 6, heading["y"] - 14, card["width"] + 12, card["y"] + card["height"] - heading["y"] + 20)


@part("architecture")
def architecture(env: cs.Env) -> None:
    """Architecture page: where each component runs today, which Azure service it is, and the setting that switches it."""
    cs.open_view(env, "/architecture", "#modes-title")
    cs.fit(env)
    save_boxes(env, "architecture", [cs.card("modes-title")])


# ---- part 3: one call, end to end ----------------------------------------------------------------------------------
@part("a")
def scenario_a(env: cs.Env) -> None:
    """Scenario A in the UI, then the hero call's review screen stage by stage, before and after the approval."""
    page = env.page
    cs.open_view(env, "/live", "#scenario-a")
    if not has_recordings(env):
        print(NO_RECORDINGS)
        return
    cs.fit(env)
    save_boxes(env, "a-picker", ["#scenario-a"])
    page.get_by_role("button", name=re.compile(r"^Process")).click()
    run = wait_ready(env, "#scenario-a")
    save_boxes(env, "a-run", [f"#scenario-a {RUN}"])

    run.get_by_role("link", name="Open full review").click()
    page.wait_for_selector("#tasks-title")
    env.ids["hero"] = page.url.rsplit("/", 1)[-1]
    page.wait_for_timeout(800)
    cs.fit(env)

    # before the approval: the stages up to the review, and the evidence quotes the working copy still allows
    pipeline_rows(env, "stage-ingest", [0, 1, 2])
    save_boxes(env, "stage-transcript", [cs.card("transcript-title")])
    pipeline_rows(env, "stage-understand", [3, 4, 5, 6])
    open_details(env, cs.card("note-title"), "Raw note")
    save_boxes(env, "stage-note", [cs.card("note-title")])
    open_details(env, cs.card("products-title"), "Evidence")
    save_boxes(env, "stage-products", [cs.card("products-title")])
    open_details(env, cs.card("tasks-title"), "Evidence")
    save_boxes(env, "stage-tasks", [cs.card("topics-title"), cs.card("tasks-title")])
    save_boxes(env, "stage-crm-match", [cs.card("crm-match-title")])
    save_boxes(env, "stage-approve", [aside_card(env, 0)])

    # the one click, then what it wrote
    page.get_by_role("button", name=re.compile(r"Approve & send to CRM")).click()
    page.get_by_role("heading", name="Written to the CRM").wait_for(timeout=60_000)
    page.wait_for_timeout(5_500)               # the "Approved and sent to the CRM" toast stays on screen for a few seconds
    cs.fit(env)
    save_boxes(env, "stage-written", [aside_card(env, 0)])
    save_boxes(env, "stage-pipeline", [PIPELINE])
    pipeline_rows(env, "stage-review", [7, 8, 9], footer=True)
    save_boxes(env, "stage-retention", [cs.card("retention-title")])


# ---- part 4: the policies ------------------------------------------------------------------------------------------
@part("policies")
def policies(env: cs.Env) -> None:
    """The Policies page, then one call with a card number, a medical aside and small talk, and the same call in two markets."""
    page = env.page
    cs.open_view(env, "/policies", "#processing-title")
    cs.fit(env)
    save_boxes(env, "policy-markets", ['section[aria-labelledby="markets-heading"]'])
    save_boxes(env, "policy-notes", [cs.card("notes-policy-title")])
    page.locator("summary", has_text="Routing: transcript source").click()
    cs.fit(env)
    save_boxes(env, "policy-processing", [cs.card("processing-title")])

    open_review(env, run_call(env, "privacy", PRIVACY_CALL))
    save_boxes(env, "policy-privacy", [cs.card("privacy-title")])
    page.evaluate(SCROLL_TRANSCRIPT, 7)
    save_boxes(env, "policy-omitted", [cs.card("transcript-title")])
    page.evaluate(SCROLL_TRANSCRIPT, 19)
    save_boxes(env, "policy-redacted", [cs.card("transcript-title")])
    save_boxes(env, "policy-retention-us", [cs.card("retention-title")])

    open_review(env, run_call(env, "privacy-eu", PRIVACY_CALL, market="EU"))
    save_boxes(env, "policy-retention-eu", [cs.card("retention-title")])


# ---- part 5: a Teams call ------------------------------------------------------------------------------------------
@part("b")
def scenario_b(env: cs.Env) -> None:
    """Scenario B: a simulated Teams call (the platform transcript path) on live Language and model services."""
    page = env.page
    cs.open_view(env, "/live", "#scenario-b")
    picker = page.locator("#scenario-b select").first
    picker.select_option(value=page.locator("#scenario-b select option", has_text="ETF").first.get_attribute("value"))
    page.get_by_role("button", name="Simulate a Teams call").click()
    run = wait_ready(env, "#scenario-b")
    run.get_by_role("button", name="Approve and write to the CRM").click()
    run.get_by_text("Written to the CRM:").wait_for(timeout=60_000)
    page.wait_for_timeout(5_500)
    cs.fit(env)
    save_boxes(env, "b-run", [f"#scenario-b {RUN}"])


# ---- part 6: hundreds of calls -------------------------------------------------------------------------------------
@part("c")
def scenario_c(env: cs.Env) -> None:
    """Scenario C: the billed-run confirmation (800 calls, dismissed), then a real 50-call run and its report."""
    page = env.page
    cs.open_view(env, "/live", "#scenario-c")
    page.get_by_label("Number of calls").fill("800")
    page.get_by_role("button", name=re.compile(r"^Start 800-call run")).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Start a run that is billed?").wait_for(timeout=30_000)
    page.wait_for_timeout(600)
    guard(env)
    dialog.screenshot(path=env.out / "walkthrough-c-confirm.png")
    log("walkthrough-c-confirm.png")
    page.keyboard.press("Escape")

    page.get_by_label("Number of calls").fill("50")
    page.wait_for_timeout(800)
    cs.fit(env)
    save_boxes(env, "c-setup", [cs.card("bulk-settings-title")])
    page.get_by_role("button", name=re.compile(r"^Start 50-call run")).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Start a run that is billed?").wait_for(timeout=30_000)
    dialog.get_by_role("button", name=re.compile(r"start|confirm|run", re.IGNORECASE)).last.click()
    page.locator(cs.card("load-report-title")).wait_for(timeout=300_000)
    page.wait_for_timeout(1_500)
    cs.fit(env)
    save_boxes(env, "c-report", [cs.card("load-report-title")])
    cs.open_view(env, "/", "section[aria-labelledby='load-title']")
    page.wait_for_timeout(1_500)
    save_boxes(env, "c-inbox", [cs.card("load-title")])


# ---- part 8: the Record button --------------------------------------------------------------------------------------
# Remembers what the page uploads, so the same recording can be sent again with other form values.
CAPTURE_UPLOADS = r"""
(() => {
  window.__uploads = [];
  const grab = (body, url) => {
    if (!(body instanceof FormData) || !String(url).includes('/ingest/upload')) return;
    const fields = {};
    let file = null;
    for (const [key, value] of body.entries()) { if (value instanceof Blob) file = value; else fields[key] = String(value); }
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => window.__uploads.push({ fields, name: file.name, type: file.type, b64: String(reader.result).split(',')[1] });
    reader.readAsDataURL(file);
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__url = url; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) { grab(body, this.__url); return send.apply(this, arguments); };
})();
"""


def launch_with_fake_mic(pw: Playwright) -> Browser:
    """Chrome or Edge whose microphone is the hero recording, played once from the moment recording starts."""
    args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={HERO_WAV.as_posix()}%noloop"]
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=channel, args=args)
        except Exception:
            continue
    raise RuntimeError("no Chrome or Edge found: Chromium from Playwright cannot be used here (run: python -m playwright install chrome)")


def record_button(env: cs.Env, seconds: int) -> None:
    """The in-person route end to end: fill the form, record, save and upload, then look at what came out."""
    page = env.page
    cs.open_view(env, "/capture", 'button[aria-label="Start recording"]')
    page.fill("#cap-org", "Harborview Pension Fund")
    page.fill("#cap-name", "Dana Whitfield")
    page.locator("summary", has_text="Your details").click()
    page.fill("#cap-seller-name", "Sam Rivera")
    page.fill("#cap-seller-email", "sam.rivera@contoso.example")
    cs.fit(env)
    save_page(env, "capture-form")

    page.click('button[aria-label="Start recording"]')
    page.wait_for_selector('button[aria-label="Stop recording"]')
    started = time.time()
    page.wait_for_timeout(9_000)
    save_page(env, "capture-recording")
    page.wait_for_timeout(max(0.0, seconds - (time.time() - started)) * 1000)
    page.click('button[aria-label="Stop recording"]')
    page.wait_for_selector("text=Save & upload")
    cs.fit(env)
    save_page(env, "capture-recorded")

    page.click("button:has-text('Save & upload')")
    page.wait_for_selector("text=Uploaded", timeout=180_000)
    page.wait_for_timeout(5_500)              # the "Saved: uploading now" toast stays on screen for a few seconds
    cs.fit(env)
    save_page(env, "capture-uploaded")
    href = page.locator("a:has-text('Open the review')").first.get_attribute("href")
    cid = href.rsplit("/", 1)[-1]
    sent = page.evaluate("window.__uploads[0]")

    api = http(env)
    detail = wait_processed(api, cid)
    record = detail["record"]
    if record.get("transcript_origin") != "azure_speech":
        raise RuntimeError(f"the recording was transcribed by {record.get('transcript_origin')}, not Azure Speech: is Speech live in .env?")
    log(f"recorded {seconds}s -> {record['status']}, {record['audio_seconds']:.0f}s of audio, "
        f"CRM match {(record['proposal']['crm_match'].get('account') or {}).get('name')}")

    cs.open_view(env, "/", "#main li.card")
    cs.fit(env)
    save_boxes(env, "record-inbox", ["#main li.card"])
    cs.open_view(env, href, "#tasks-title")
    cs.fit(env)
    save_boxes(env, "record-review", [cs.card("note-title"), cs.card("action-title"), cs.card("crm-match-title")])

    # the same recording again with the form left on its defaults: what skipping the form costs
    blank = {"channel": "mobile", "market": "US", "business_line": "professional_buyer", "seller_name": "Demo Seller",
             "seller_email": "demo.seller@contoso.example", "title": "In-person visit", "started_at": sent["fields"]["started_at"],
             "source_interaction_id": "walkthrough-blank-form"}
    reply = api.post("/ingest/upload", data=blank, files={"file": (sent["name"], base64.b64decode(sent["b64"]), sent["type"])})
    blank_cid = reply.raise_for_status().json()["conversation_id"]
    wait_processed(api, blank_cid)
    cs.open_view(env, "/", "#main li.card")
    cs.fit(env)
    save_boxes(env, "record-no-crm-inbox", ["#main li.card"])          # newest first: this is the blank-form one
    cs.open_view(env, f"/conversations/{blank_cid}", "#tasks-title")
    cs.fit(env)
    save_boxes(env, "record-no-crm", [cs.card("crm-match-title")])


def record_part(pw: Playwright, base: str, out: Path, seconds: int) -> None:
    if not HERO_WAV.exists():
        print(NO_RECORDINGS)
        return
    browser = launch_with_fake_mic(pw)
    try:
        context = browser.new_context(permissions=["microphone"], viewport={"width": cs.WIDTH, "height": 900})
        page = context.new_page()
        page.add_init_script(CAPTURE_UPLOADS)
        print(f"recording the hero call through the Record button ({seconds} s) ...", flush=True)
        record_button(cs.Env(base, {}, out, browser, page), seconds)
    finally:
        browser.close()


# ---- part 9: the rest of the product --------------------------------------------------------------------------------
@part("product")
def product(env: cs.Env) -> None:
    """The top-3 pick list, a former fund name (no candidate, then taught, then resolved) and the learned-alias table."""
    page = env.page
    open_review(env, run_call(env, "pick", "pb-ambiguous-equity-fund"))
    save_boxes(env, "pick", [cs.card("products-title")])

    api = http(env)
    first = run_call(env, "legacy", "pb-teams-legacy-blue-chip")
    open_review(env, first)
    save_boxes(env, "legacy", [cs.card("products-title")])
    mention = api.get(f"/conversations/{first}").json()["record"]["proposal"]["result"]["products"][0]["mention_id"]
    api.post(f"/conversations/{first}/approve", json={"product_choices": {mention: "SYN-000002"}, "clicks": 3}).raise_for_status()
    open_review(env, run_call(env, "legacy-again", "pb-teams-legacy-blue-chip"))
    save_boxes(env, "legacy-resolved", [cs.card("products-title")])

    cs.open_view(env, "/catalogue", "#aliases-title")
    page.fill("#catalogue-q", "equity fund class c")
    page.wait_for_timeout(2_000)
    cs.fit(env)
    search = box(env, cs.card("search-title"))
    save_clip(env, "catalogue-search", search["x"] - 6, search["y"] - 6, search["width"] + 12, min(search["height"], 640) + 12)
    save_boxes(env, "aliases", [cs.card("aliases-title")])


@part("engines")
def engines(env: cs.Env) -> None:
    """The same call under each engine: products, topics and tasks come from the rules; the model only changes how it is written."""
    page = env.page
    open_review(env, run_call(env, "engines", HERO))
    for engine in ("hybrid", "rules", "llm"):
        if engine != "hybrid":
            page.select_option("#engine-select", engine)
            page.get_by_text(f"Engine: {engine}").first.wait_for(timeout=90_000)
            page.wait_for_timeout(1_200)
            cs.fit(env)
        save_boxes(env, f"engine-{engine}", [cs.card("note-title")])


@part("inbox")
def inbox(env: cs.Env) -> None:
    """The Inbox with a mix of calls: waiting ones (a pick, a call with something to remove) and three already written."""
    run_call(env, "pick", "pb-ambiguous-equity-fund")
    run_call(env, "privacy", PRIVACY_CALL)
    for key, fixture in CRM_CALLS:
        run_call(env, key, fixture, approve=True)
    cs.open_view(env, "/", "#main li.card")
    last = min(4, env.page.locator("#main li.card").count() - 1)
    bottom = box(env, env.page.locator("#main li.card").nth(last))["y"] + box(env, env.page.locator("#main li.card").nth(last))["height"] + 12
    env.page.set_viewport_size({"width": cs.WIDTH, "height": math.ceil(bottom) + 20})
    env.page.wait_for_timeout(300)
    save_top(env, "inbox", bottom)


@part("crm")
def crm(env: cs.Env) -> None:
    """What landed in the CRM simulator: the phone call with its note, the tasks, and the structured insight."""
    page = env.page
    for key, fixture in CRM_CALLS:
        run_call(env, key, fixture, approve=True)
    cs.open_view(env, "/crm", "role=tab")
    for tab, name in (("Phone calls", "crm-phonecalls"), ("Tasks", "crm-tasks"), ("Insights", "crm-insights")):
        page.get_by_role("tab", name=tab).click()
        page.wait_for_selector("#main [role=tabpanel] tbody tr, #main [role=tabpanel] ul.grid > li")
        page.wait_for_timeout(500)
        cs.fit(env)
        panel = box(env, "#main [role=tabpanel]")
        save_top(env, name, panel["y"] + min(panel["height"], 470))


@part("sources")
def sources(env: cs.Env) -> None:
    """Sources: how each channel connects, the recordings you can run, and the upload form."""
    cs.open_view(env, "/sources", "#channels-title")
    cs.fit(env)
    save_boxes(env, "sources", [cs.card("channels-title")])
    simulate = box(env, cs.card("simulate-title"))
    save_clip(env, "simulate", simulate["x"] - 6, simulate["y"] - 6, simulate["width"] + 12, min(simulate["height"], 640) + 12)
    save_boxes(env, "upload", [cs.card("upload-title")])


@part("scoreboard")
def scoreboard(env: cs.Env) -> None:
    """The evaluation harness: the rules engine on the scripted golden set, before and after learning.

    It makes no model call, but the privacy filter still uses the live Azure AI Language service (a few cents per run).
    """
    api = http(env)
    for body in ({"engine": "rules", "label": "Baseline"}, {"engine": "rules", "apply_learning": True, "label": "With learning"}):
        api.post("/eval/run", json=body).raise_for_status()
    cs.open_view(env, "/scoreboard", "#compare-title")
    cs.fit(env)
    metrics = box(env, 'section[aria-label="Metrics for the selected run"]')
    save_top(env, "scoreboard", metrics["y"] + metrics["height"] + 16)


# ---- the reset ------------------------------------------------------------------------------------------------------
@part("reset")
def reset_demo(env: cs.Env) -> None:
    """Live demo, last section: what a reset would remove, the confirmation, and the empty app afterwards.

    Runs last: it really resets the throwaway server (never yours), so nothing after it could use the calls it removes.
    """
    page = env.page
    api = http(env)
    run_call(env, "crm-teams", "pb-teams-etf-vs-mutual-fund", approve=True)          # a call written to the CRM
    run_call(env, "pick", "pb-ambiguous-equity-fund")                                  # a call waiting for a pick
    if not api.get("/aliases").json():                                                 # a learned product name
        first = run_call(env, "legacy", "pb-teams-legacy-blue-chip")
        mention = api.get(f"/conversations/{first}").json()["record"]["proposal"]["result"]["products"][0]["mention_id"]
        api.post(f"/conversations/{first}/approve", json={"product_choices": {mention: "SYN-000002"}, "clicks": 3}).raise_for_status()
    processing = api.get("/policies").json()["processing"]                             # a policy edit
    api.put("/policies/processing", json={**processing, "top_k_products": 5}).raise_for_status()

    cs.open_view(env, "/live", "#demo-data-title")
    page.get_by_text("Nothing to reset").wait_for(state="detached", timeout=30_000)
    cs.fit(env)
    save_boxes(env, "reset-card", ["#demo-data"])

    page.locator(cs.card("demo-reset-title")).get_by_role("button", name="Reset demo data").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Reset the demo data?").wait_for(timeout=30_000)
    page.wait_for_timeout(600)
    guard(env)
    dialog.screenshot(path=env.out / "walkthrough-reset-confirm.png")
    log("walkthrough-reset-confirm.png")
    dialog.get_by_role("button", name="Reset demo data").click()
    page.get_by_text("Nothing to reset").first.wait_for(timeout=60_000)
    page.wait_for_timeout(800)
    cs.fit(env)
    save_boxes(env, "reset-done", ["#demo-data"])

    cs.open_view(env, "/", "text=No conversations yet")
    page.wait_for_timeout(800)
    empty = box(env, page.locator("#main .card").last)
    save_top(env, "reset-inbox", empty["y"] + empty["height"] + 12)


# Run in this order: later parts reuse what earlier ones created (the Inbox and the CRM show the live calls).
ORDER = ["services", "architecture", "a", "engines", "policies", "b", "product", "record", "inbox", "crm", "sources", "scoreboard", "c", "reset"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="", help=f"comma-separated subset of: {', '.join(ORDER)}")
    parser.add_argument("--seconds", type=int, default=HERO_SECONDS, help="how long the record part records (the hero call is 187 s)")
    parser.add_argument("--out", type=Path, default=cs.ROOT / "docs" / "screenshots")
    parser.add_argument("--base-url", default="", help="develop against a scratch server you started yourself instead of a throwaway one; "
                        "the parts run real calls and approve them, so never point this at a server that holds real data")
    args = parser.parse_args()
    wanted = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in ORDER]
    if unknown:
        parser.error(f"unknown part(s): {', '.join(unknown)}")
    if not (cs.ROOT / "frontend" / "dist" / "index.html").exists():
        parser.error("frontend\\dist is missing: run `npm run build` in frontend first")
    args.out.mkdir(parents=True, exist_ok=True)
    if not DENY:
        print("note: .deny-terms.local is missing or empty, so only the generic checks run before an image is saved")

    server = contextlib.nullcontext(args.base_url) if args.base_url else cs.throwaway_server(live=True)
    with server as base, sync_playwright() as pw:
        print(f"live API on {base}", flush=True)
        browser = cs.launch(pw)
        try:
            env = cs.Env(base, {}, args.out, browser, browser.new_page(viewport={"width": cs.WIDTH, "height": 900}))
            for name in ORDER:
                if wanted and name not in wanted:
                    continue
                print(f"[{name}]", flush=True)
                if name == "record":
                    record_part(pw, base, args.out, args.seconds)
                else:
                    PARTS[name](env)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Generates docs/diagrams/voicecrm-architecture-v2.{excalidraw,png} (native Excalidraw JSON + a PNG preview).

Run from the repo root:  .\.venv\Scripts\python.exe tools\gen_architecture_diagram.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"
NAME = "voicecrm-architecture-v2"
FONT = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
LH = 1.25
random.seed(11)

elements: list[dict] = []
bold_ids: set[str] = set()
_counter = 0


def nid(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}"


def measure(text: str, size: float, bold: bool = False) -> float:
    font = ImageFont.truetype(FONT_BOLD if bold else FONT, int(round(size * 4)))
    return font.getlength(text) / 4


def base(kind: str, x: float, y: float, w: float, h: float, **kw) -> dict:
    el = {
        "id": kw.pop("id", None) or nid(kind), "type": kind, "x": round(x, 2), "y": round(y, 2),
        "width": round(w, 2), "height": round(h, 2), "angle": 0,
        "strokeColor": kw.pop("strokeColor", "#1e1e1e"), "backgroundColor": kw.pop("backgroundColor", "transparent"),
        "fillStyle": "solid", "strokeWidth": kw.pop("strokeWidth", 2), "strokeStyle": kw.pop("strokeStyle", "solid"),
        "roughness": 0, "opacity": 100, "groupIds": kw.pop("groupIds", []), "frameId": None,
        "roundness": kw.pop("roundness", None), "seed": random.randint(1, 2**31 - 1), "version": 1,
        "versionNonce": random.randint(1, 2**31 - 1), "isDeleted": False, "boundElements": [], "updated": 1,
        "link": None, "locked": False,
    }
    el.update(kw)
    return el


def text(x, y, w, content, size, color="#1e1e1e", align="center", group=None, bold=False) -> dict:
    lines = content.split("\n")
    el = base("text", x, y, w, len(lines) * size * LH, strokeColor=color, strokeWidth=1,
              groupIds=[group] if group else [], text=content, fontSize=size, fontFamily=2, textAlign=align,
              verticalAlign="top", containerId=None, originalText=content, autoResize=True, lineHeight=LH)
    if bold:
        bold_ids.add(el["id"])
    elements.append(el)
    return el


def rect(x, y, w, h, fill, stroke, sw=2, group=None) -> dict:
    el = base("rectangle", x, y, w, h, strokeColor=stroke, backgroundColor=fill, strokeWidth=sw,
              roundness={"type": 3}, groupIds=[group] if group else [])
    elements.append(el)
    return el


def arrow(points_abs, start=None, end=None, dashed=False, color="#495057", width=2) -> dict:
    sx, sy = points_abs[0]
    rel = [[round(px - sx, 2), round(py - sy, 2)] for px, py in points_abs]
    xs, ys = [p[0] for p in rel], [p[1] for p in rel]
    el = base("arrow", sx, sy, max(xs) - min(xs), max(ys) - min(ys), strokeColor=color, strokeWidth=width,
              strokeStyle="dashed" if dashed else "solid", points=rel, lastCommittedPoint=None, startBinding=None,
              endBinding=None, startArrowhead=None, endArrowhead="arrow", elbowed=False)
    for side, target in (("startBinding", start), ("endBinding", end)):
        if target is not None:
            el[side] = {"elementId": target["id"], "focus": 0, "gap": 4}
            target["boundElements"].append({"id": el["id"], "type": "arrow"})
    elements.append(el)
    return el


def elbow(sx, sy, ex, ey, mx, **kw) -> dict:
    pts = [(sx, sy), (ex, ey)] if abs(sy - ey) < 0.5 else [(sx, sy), (mx, sy), (mx, ey), (ex, ey)]
    return arrow(pts, **kw)


M, CW, GAP = 50, 320, 90
XS = [M + i * (CW + GAP) for i in range(5)]
FY, HDR, BODY_H = 175, 74, 588
FH = HDR + BODY_H + 24
BY = FY + HDR
PAD = 16
BW = CW - 2 * PAD
TOTAL_W = 5 * CW + 4 * GAP

PALETTE = {
    "source": ("#fff4e6", "#e8590c", "#7c2d12"), "ingest": ("#e7f5ff", "#1971c2", "#0b3d6e"),
    "ai": ("#f3f0ff", "#6741d9", "#3b1f8f"), "act": ("#e3fafc", "#0c8599", "#06515e"),
    "dest": ("#ebfbee", "#2f9e44", "#175c27"),
}

COLS = [
    dict(title="1 · Capture", sub="Where conversations happen", kind="source", h=100, gap=22, boxes=[
        ("Microsoft Teams", "meetings & calls\nGraph transcript (included) or audio"),
        ("Zoom", "outbound calls\ncloud-recording audio (M4A)"),
        ("Genesys Cloud", "inbound calls\nSTA transcript if free, else audio"),
        ("Mobile / in-person", "PWA: record offline, sync later\nphoto capture = bonus"),
        ("Other tools", "Claude / ChatGPT flows, Jump-style apps\ntranscript or audio via API"),
    ]),
    dict(title="2 · Ingest & transcribe", sub="One processor, no double paying", kind="ingest", h=120, gap=36, boxes=[
        ("Source adapters", "Graph · webhooks · notifications · upload\nverify · de-dupe on source ID"),
        ("Transcription policy", "per channel: platform transcript if free,\nelse audio → Azure Speech (EN + ES)"),
        ("Vault - never the CRM", "audio deleted after transcription\ntranscript kept only if market = ON"),
        ("Canonical conversation", "speakers · timestamps · source ID\nmarket · business line · consent"),
    ]),
    dict(title="3 · Understand", sub="Filter, extract, resolve, validate", kind="ai", h=120, gap=36, boxes=[
        ("Notes policy", "redact card / ID · omit medical\ndrop small talk · keep rapport facts"),
        ("Extract (LLM or rules)*", "Foundry structured output (LLM)\nor deterministic rules engine"),
        ("Product + topic resolver", "catalogue index · exact share class\ntop-3 pick list · learned aliases"),
        ("Validate", "no leaks · evidence quotes\ncatalogue + taxonomy IDs only"),
    ]),
    dict(title="4 · Review & write", sub="One click, then it is done", kind="act", h=170, gap=39, boxes=[
        ("One-click review", "approve · edit only if needed\nauto-approve dial toward zero-touch"),
        ("Retention policy engine", "per market: transcript ON/OFF · days\nsweep + purge, audit explanation"),
        ("CRM writer", "idempotent upsert · retry on 429\ntasks = seller / internal commitments"),
    ]),
    dict(title="5 · Where it lands", sub="What sellers and analysts use", kind="dest", h=170, gap=39, boxes=[
        ("Sales agent (M365 Copilot)", "optional · Teams-only seller experience\nreads CRM + Graph data"),
        ("Snowflake data hub", "existing CRM → Snowflake feed\nthe analytics team mines it"),
        ("Dynamics 365 / Dataverse", "call note · tasks · products · topics\nstructured insight · no transcript"),
    ]),
]

text(M, 16, 1500, "VoiceCRM - conversation-to-CRM hub on Azure", 32, "#1e1e1e", "left", bold=True)
text(M, 60, 1950, "Any channel → one Microsoft-controlled processor → governed extraction → one-click review → CRM "
     "(no transcripts in the CRM)", 16, "#495057", "left")

for c, col in enumerate(COLS):
    fill, stroke, tcol = PALETTE[col["kind"]]
    rect(XS[c], FY, CW, FH, "#f8f9fa", "#ced4da", sw=1)
    text(XS[c] + 18, FY + 14, CW - 36, col["title"], 22, tcol, "left", bold=True)
    text(XS[c] + 18, FY + 46, CW - 36, col["sub"], 13, "#868e96", "left")

boxes: dict[tuple[int, int], dict] = {}
overflow = []
for c, col in enumerate(COLS):
    fill, stroke, tcol = PALETTE[col["kind"]]
    for i, (title, body) in enumerate(col["boxes"]):
        x, y = XS[c] + PAD, BY + i * (col["h"] + col["gap"])
        gid = nid("grp")
        boxes[(c, i)] = rect(x, y, BW, col["h"], fill, stroke, group=gid)
        t_h, b_h = 17 * LH, (body.count("\n") + 1) * 14 * LH
        top = y + (col["h"] - (t_h + 6 + b_h)) / 2
        text(x + 10, top, BW - 20, title, 17, tcol, "center", group=gid, bold=True)
        text(x + 10, top + t_h + 6, BW - 20, body, 14, "#495057", "center", group=gid)
        widest = max([measure(title, 17, True)] + [measure(line, 14) for line in body.split("\n")])
        if widest > BW - 24:
            overflow.append((title, round(widest), BW - 24))

G = 4
adapters = boxes[(1, 0)]
for i in range(5):
    src = boxes[(0, i)]
    elbow(src["x"] + src["width"] + G, src["y"] + src["height"] / 2, adapters["x"] - G, adapters["y"] + 20 + 20 * i,
          XS[0] + CW + GAP / 2 - 5, start=src)
for c, n in ((1, 4), (2, 4), (3, 3)):
    for i in range(n - 1):
        a, b = boxes[(c, i)], boxes[(c, i + 1)]
        cx = a["x"] + a["width"] / 2
        arrow([(cx, a["y"] + a["height"] + G), (cx, b["y"] - G)], start=a, end=b)
canon, prep = boxes[(1, 3)], boxes[(2, 0)]
elbow(canon["x"] + BW + G, canon["y"] + 60, prep["x"] - G, prep["y"] + 60, XS[1] + CW + GAP / 2, start=canon, end=prep)
valid, review = boxes[(2, 3)], boxes[(3, 0)]
elbow(valid["x"] + BW + G, valid["y"] + 60, review["x"] - G, review["y"] + 85, XS[2] + CW + GAP / 2, start=valid, end=review)
writer, d365, snow, sales = boxes[(3, 2)], boxes[(4, 2)], boxes[(4, 1)], boxes[(4, 0)]
y = writer["y"] + writer["height"] / 2
arrow([(writer["x"] + BW + G, y), (d365["x"] - G, y)], start=writer, end=d365)
cx = d365["x"] + BW / 2
arrow([(cx, d365["y"] - G), (cx, snow["y"] + snow["height"] + G)], start=d365, end=snow)

teams = boxes[(0, 0)]
ty, vx = teams["y"] + teams["height"] / 2, XS[4] + CW - 50
arrow([(teams["x"] - G, ty), (26, ty), (26, 138), (vx, 138), (vx, sales["y"] - G)], start=teams, dashed=True,
      color="#e8590c", width=2.5)
text(70, 112, 1700, "Native path (Teams only, no build): Sales agent in Microsoft 365 Copilot - prep, recap, tasks, follow-up drafts",
     14, "#d9480f", "left")

band_y = FY + FH + 24
rect(M, band_y, TOTAL_W, 112, "#f1f3f5", "#868e96", sw=1)
text(M + 18, band_y + 12, TOTAL_W - 36, "Cross-cutting", 17, "#343a40", "left", bold=True)
band_lines = (
    "Governance: Entra ID · managed identity · Key Vault · Private Link · Purview labels · region / data-zone control · synthetic data only in the POC\n"
    "Quality: golden-set scoreboard (product top-1 / top-3, share-class confusion, topic F1, leaks = 0) · learned aliases · p50 / p95 latency\n"
    "Economics and config: per-call meter (audio minutes, tokens) → cost per call (illustrative) · templates, taxonomy, markets, thresholds are data"
)
text(M + 18, band_y + 40, TOTAL_W - 36, band_lines, 14, "#495057", "left")
for line in band_lines.split("\n"):
    if measure(line, 14) > TOTAL_W - 40:
        overflow.append(("band", round(measure(line, 14)), TOTAL_W - 40))

ly, lx = band_y + 112 + 18, M
for label, kind in (("Source system", "source"), ("Ingest / transcribe", "ingest"), ("Understanding & validation", "ai"),
                    ("Review & write", "act"), ("Destination / consumer", "dest")):
    fill, stroke, _ = PALETTE[kind]
    rect(lx, ly, 16, 16, fill, stroke, sw=2)
    text(lx + 24, ly - 1, measure(label, 13) + 6, label, 13, "#495057", "left")
    lx += 24 + measure(label, 13) + 34
arrow([(lx, ly + 8), (lx + 44, ly + 8)], dashed=True, color="#e8590c", width=2.5)
text(lx + 54, ly - 1, 300, "Native Sales agent path (Teams only)", 13, "#495057", "left")
text(M, ly + 30, TOTAL_W, "* LLM or rules: a Foundry model with structured output, or the no-LLM rules engine; hybrid = rules candidates "
     "+ LLM summary. Model output is always reconciled against the catalogue and taxonomy.", 13, "#868e96", "left")

OUT.mkdir(parents=True, exist_ok=True)
doc = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com", "elements": elements,
       "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"}, "files": {}}
(OUT / f"{NAME}.excalidraw").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def render_png(path: Path, scale: float = 1.5) -> None:
    img = Image.new("RGB", (int((M * 2 + TOTAL_W) * scale), int((ly + 60) * scale)), "white")
    d = ImageDraw.Draw(img)
    fonts: dict = {}

    def font(size, bold=False):
        key = (round(size * scale), bold)
        if key not in fonts:
            fonts[key] = ImageFont.truetype(FONT_BOLD if bold else FONT, key[0])
        return fonts[key]

    def dashed_line(p0, p1, color, w):
        (x0, y0), (x1, y1) = p0, p1
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if length == 0:
            return
        dash, gap = 10 * scale, 7 * scale
        pos = 0.0
        while pos < length:
            end = min(pos + dash, length)
            d.line([(x0 + (x1 - x0) * pos / length, y0 + (y1 - y0) * pos / length),
                    (x0 + (x1 - x0) * end / length, y0 + (y1 - y0) * end / length)], fill=color, width=w)
            pos += dash + gap

    for el in elements:
        kind = el["type"]
        if kind == "rectangle":
            x, y, w, h = (el[k] * scale for k in ("x", "y", "width", "height"))
            fill = None if el["backgroundColor"] == "transparent" else el["backgroundColor"]
            d.rounded_rectangle([x, y, x + w, y + h], radius=12 * scale if el["roundness"] else 0, fill=fill,
                                outline=el["strokeColor"], width=max(1, int(el["strokeWidth"] * scale)))
        elif kind == "text":
            size, is_bold = el["fontSize"], el["id"] in bold_ids
            for n, line in enumerate(el["text"].split("\n")):
                ty_ = (el["y"] + n * size * LH) * scale
                if el["textAlign"] == "center":
                    d.text(((el["x"] + el["width"] / 2) * scale, ty_), line, font=font(size, is_bold),
                           fill=el["strokeColor"], anchor="ma")
                else:
                    d.text((el["x"] * scale, ty_), line, font=font(size, is_bold), fill=el["strokeColor"], anchor="la")
        elif kind == "arrow":
            pts = [((el["x"] + px) * scale, (el["y"] + py) * scale) for px, py in el["points"]]
            lw = max(1, int(el["strokeWidth"] * scale))
            for a, b in zip(pts, pts[1:], strict=False):
                dashed_line(a, b, el["strokeColor"], lw) if el["strokeStyle"] == "dashed" else d.line([a, b], fill=el["strokeColor"], width=lw)
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
            dx, dy = x1 - x0, y1 - y0
            norm = (dx * dx + dy * dy) ** 0.5 or 1
            ux, uy = dx / norm, dy / norm
            hl, hw = 11 * scale, 5.5 * scale
            d.polygon([(x1, y1), (x1 - ux * hl - uy * hw, y1 - uy * hl + ux * hw),
                       (x1 - ux * hl + uy * hw, y1 - uy * hl - ux * hw)], fill=el["strokeColor"])
    img.save(path)


render_png(OUT / f"{NAME}.png")
print("elements:", len(elements), "| overflow warnings:", overflow or "none")

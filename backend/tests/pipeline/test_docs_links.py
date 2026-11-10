"""The documentation a reader clicks through: every local image exists, and every link and anchor in the walkthrough resolves."""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from voicecrm.config import REPO_ROOT

DOCS = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
WALKTHROUGH = REPO_ROOT / "docs" / "walkthrough.md"
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)|<img\s[^>]*\bsrc=\"([^\"]+)\"")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    return frozenset(slug(h) for h in HEADING.findall(path.read_text(encoding="utf-8")))


def local(target: str) -> bool:
    return not re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_local_image_exists(doc: Path) -> None:
    refs = [a or b for a, b in IMAGE.findall(doc.read_text(encoding="utf-8"))]
    missing = [ref for ref in refs if local(ref) and not (doc.parent / ref).resolve().exists()]
    assert not missing


def test_the_walkthrough_shows_a_screenshot_for_every_scenario() -> None:
    images = {Path(a or b).name for a, b in IMAGE.findall(WALKTHROUGH.read_text(encoding="utf-8"))}
    assert {"walkthrough-services.png", "walkthrough-a-run.png", "walkthrough-stage-ingest.png", "walkthrough-stage-understand.png",
            "walkthrough-stage-written.png", "walkthrough-policy-privacy.png", "walkthrough-policy-retention-eu.png",
            "walkthrough-b-run.png", "walkthrough-c-report.png", "walkthrough-capture-recorded.png",
            "walkthrough-record-review.png"} <= images


def test_every_walkthrough_image_on_disk_is_used_by_the_guide() -> None:
    used = {Path(a or b).name for a, b in IMAGE.findall(WALKTHROUGH.read_text(encoding="utf-8"))}
    on_disk = {p.name for p in (REPO_ROOT / "docs" / "screenshots").glob("walkthrough-*.png")}
    assert on_disk - used == set(), "images the guide no longer shows: delete them"


def test_every_link_in_the_walkthrough_resolves() -> None:
    broken = []
    for target in LINK.findall(WALKTHROUGH.read_text(encoding="utf-8")):
        if not local(target):
            continue
        file_part, _, anchor = target.partition("#")
        path = (WALKTHROUGH.parent / file_part).resolve() if file_part else WALKTHROUGH
        if not path.exists() or (anchor and path.suffix == ".md" and anchor not in anchors(path)):
            broken.append(target)
    assert not broken


def test_the_walkthrough_table_of_contents_covers_every_part() -> None:
    text = WALKTHROUGH.read_text(encoding="utf-8")
    parts = re.findall(r"^## (\d+)\. ", text, re.MULTILINE)
    linked = re.findall(r"^\| \[(\d+)\]\(#", text, re.MULTILINE)
    assert parts == [str(n) for n in range(1, len(parts) + 1)] and linked == parts

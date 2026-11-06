"""Referential-integrity checks for the synthetic data files (catalogue, taxonomy, CRM seed, scripted calls).

Used by `tests/domain/test_data_files.py` and as a CLI for people authoring scripted calls:

    cd backend
    ..\\.venv\\Scripts\\python.exe -m voicecrm.eval.data_checks            # every file in data\\calls
    ..\\.venv\\Scripts\\python.exe -m voicecrm.eval.data_checks pb-teams-equity-class-c

A scripted call is only useful as a test when its gold labels are *checkable against its own script*: every gold
surface form, task keyword, must-keep keyword and must-not-leak string has to actually occur in the dialogue.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from ..data_loader import (
    load_catalogue, load_crm_seed, load_policies, load_scripted_calls, load_taxonomy, load_templates,
)
from ..domain.fixtures import ScriptedCall

BUSINESS_LINES = {"professional_buyer", "retail", "international"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def check_catalogue(data_dir: Path | None = None) -> list[str]:
    cat = load_catalogue(data_dir)
    errors: list[str] = []
    ids = Counter(p.product_id for p in cat.products)
    errors += [f"catalogue: duplicate product_id {k}" for k, v in ids.items() if v > 1]
    seen: set[tuple[str, str]] = set()
    for p in cat.products:
        if p.share_class not in cat.share_classes:
            errors.append(f"catalogue: {p.product_id} uses unknown share class {p.share_class!r}")
        if (p.family_id, p.share_class) in seen:
            errors.append(f"catalogue: duplicate family/class {p.family_id}/{p.share_class}")
        seen.add((p.family_id, p.share_class))
    return errors


def check_taxonomy(data_dir: Path | None = None) -> list[str]:
    tax = load_taxonomy(data_dir)
    ids = Counter(t.topic_id for t in tax.topics)
    errors = [f"taxonomy: duplicate topic_id {k}" for k, v in ids.items() if v > 1]
    errors += [f"taxonomy: {t.topic_id} has no synonyms" for t in tax.topics if not t.synonyms]
    errors += [f"taxonomy: {t.topic_id} has no Spanish synonyms" for t in tax.topics if not t.synonyms_i18n.get("es")]
    return errors


def check_call(call: ScriptedCall, data_dir: Path | None = None) -> list[str]:
    cat, tax, seed = load_catalogue(data_dir), load_taxonomy(data_dir), load_crm_seed(data_dir)
    templates, policies = load_templates(data_dir), load_policies(data_dir)
    products, topics = cat.by_id(), tax.by_id()
    fid, gold, errors = call.fixture_id, call.gold, []

    def err(msg: str) -> None:
        errors.append(f"{fid}: {msg}")

    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", fid):
        err("fixture_id must be kebab-case")
    try:
        started = datetime.fromisoformat(call.started_at.replace("Z", "+00:00"))
    except ValueError:
        err(f"started_at {call.started_at!r} is not ISO-8601")
        started = None
    if call.market not in policies.markets:
        err(f"unknown market {call.market!r}")
    if call.business_line not in BUSINESS_LINES:
        err(f"unknown business_line {call.business_line!r}")
    if gold.template_id not in templates:
        err(f"unknown template {gold.template_id!r}")
    if gold.expected_language not in {"en", "es"}:
        err("expected_language must be 'en' or 'es'")
    if gold.expect_crm_account and gold.expect_crm_account not in {a.id for a in seed.accounts}:
        err(f"expect_crm_account {gold.expect_crm_account!r} is not in the CRM seed")

    people = {call.seller.id: call.seller, **{p.id: p for p in call.customers}, **{p.id: p for p in call.internal}}
    if len(people) != 1 + len(call.customers) + len(call.internal):
        err("participant ids must be unique")
    if len(call.turns) < 8:
        err("fewer than 8 turns")
    defaults = {"seller": call.seller.id, "customer": call.customers[0].id if call.customers else None,
                "internal": call.internal[0].id if call.internal else None}
    for i, turn in enumerate(call.turns):
        sid = turn.speaker_id or defaults[turn.speaker]
        if sid not in people:
            err(f"turn {i} ({turn.speaker}) has unknown speaker_id {sid!r}")

    script = norm(" ".join(t.text for t in call.turns))
    turn_texts = [norm(t.text) for t in call.turns]

    for g in gold.products:
        p = products.get(g.product_id)
        if p is None:
            err(f"gold product {g.product_id} is not in the catalogue")
            continue
        if not g.surface_forms:
            err(f"gold product {g.product_id} has no surface_forms")
        for form in g.surface_forms:
            if norm(form) not in script:
                err(f"surface form {form!r} for {g.product_id} does not occur in the script")
        for other in g.acceptable_ids:
            if other not in products:
                err(f"acceptable id {other} is not in the catalogue")
        if g.ambiguous and not g.acceptable_ids:
            err(f"ambiguous gold product {g.product_id} needs acceptable_ids")
    for topic in gold.topics:
        if topic not in topics:
            err(f"gold topic {topic!r} is not in the taxonomy")
    if not gold.topics:
        err("no gold topics")
    for i, task in enumerate(gold.tasks):
        words = [norm(k) for k in task.keywords]
        if not words:
            err(f"gold task {i} has no keywords")
        elif gold.expected_language != "es" and not any(all(w in text for w in words) for text in turn_texts):
            # Spanish calls: the CRM task title is English, so keywords are English and cannot occur in the script
            err(f"gold task {i} keywords {task.keywords} do not all occur in a single turn")
        if task.due_date and started:
            if task.due_date < started.date() or task.due_date > (started + timedelta(days=60)).date():
                err(f"gold task {i} due date {task.due_date} is outside 0-60 days from the call")
    for s in gold.must_not_leak:
        if norm(s) not in script:
            err(f"must_not_leak {s!r} does not occur in the script (the leak test would be vacuous)")
    for s in gold.must_keep:
        if norm(s) not in script:
            err(f"must_keep {s!r} does not occur in the script")
    return errors


def check_all(data_dir: Path | None = None, only: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    if only is None:
        errors += check_catalogue(data_dir) + check_taxonomy(data_dir)
    calls = load_scripted_calls(data_dir)
    ids = Counter(c.fixture_id for c in calls)
    errors += [f"calls: duplicate fixture_id {k}" for k, v in ids.items() if v > 1]
    for call in calls:
        if only is None or call.fixture_id in only:
            errors += check_call(call, data_dir)
    return errors


def main(argv: list[str]) -> int:
    try:
        errors = check_all(only=set(argv) or None)
    except (json.JSONDecodeError, ValueError) as exc:                  # a malformed file: show it, do not traceback
        print(f"data error: {exc}")
        return 2
    for e in errors:
        print("ERROR", e)
    calls = load_scripted_calls()
    print(f"{len(calls)} scripted call(s) loaded; {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

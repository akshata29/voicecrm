"""Create the VoiceCRM custom Dataverse table ("custom" field profile) from data/crm/field_profile.yaml.

!!! UNTESTED AGAINST A LIVE ENVIRONMENT !!!  The request bodies follow the Microsoft Learn Web API metadata articles
(create table / column / relationship definitions, EntityKeyMetadata, PublishXml) and are only unit tested with mocked
HTTP. Run it against a sandbox environment first - never a production system such as MSX.

Dry-run is the default: it lists the requests it would send and touches nothing. `--apply` executes them (each step
first checks whether its target already exists, so re-running is safe). The signed-in identity (az login /
DefaultAzureCredential) needs the System Customizer role (or equivalent) in the target environment.

  python tools\\dataverse_bootstrap.py                                  # dry-run, URL from VOICECRM_DATAVERSE_URL
  python tools\\dataverse_bootstrap.py --show-bodies                    # ... and print every JSON body
  python tools\\dataverse_bootstrap.py --list-topic-values              # topic id -> choice value (to pin in the YAML)
  python tools\\dataverse_bootstrap.py --url https://<org>.crm.dynamics.com --apply

Topic choice values come from data/taxonomy/topics.json (numbered from `topic_options.base` in file order); pin them
under `topic_options.overrides` in the YAML before reordering or removing topics.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from voicecrm.adapters.crm.dataverse import CachedToken, DataverseError, DataverseHttp
from voicecrm.adapters.crm.field_profile import build_topic_option_map, default_profile_path, load_field_profile
from voicecrm.adapters.crm.schema_plan import SchemaPlan, apply_plan, build_schema_plan
from voicecrm.config import get_settings

logger = logging.getLogger("dataverse_bootstrap")

REFUSED_HOST_PREFIXES = ("microsoftsales",)       # MSX: never bootstrap schema there


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the VoiceCRM custom Dataverse table (dry-run by default).")
    parser.add_argument("--url", help="environment URL, e.g. https://<org>.crm.dynamics.com (default: VOICECRM_DATAVERSE_URL)")
    parser.add_argument("--profile", type=Path, help="field profile YAML (default: data/crm/field_profile.yaml)")
    parser.add_argument("--taxonomy", type=Path, help="topics.json used for the topic choice values")
    parser.add_argument("--apply", action="store_true", help="execute the plan (default: dry-run)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation with --apply")
    parser.add_argument("--show-bodies", action="store_true", help="print the JSON body of every step")
    parser.add_argument("--list-topic-values", action="store_true", help="print the topic -> choice value mapping and exit")
    return parser.parse_args(argv)


def load_topics(taxonomy_path: Path | None) -> list[tuple[str, str]]:
    path = taxonomy_path or get_settings().data_dir / "taxonomy" / "topics.json"
    if not path.exists():
        logger.warning("taxonomy not found at %s; the topics column is skipped", path)
        return []
    from voicecrm.domain.taxonomy import Taxonomy

    taxonomy = Taxonomy.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return [(t.topic_id, t.label) for t in taxonomy.topics]


def print_plan(plan: SchemaPlan, show_bodies: bool) -> None:
    for warning in plan.warnings:
        print(f"[warn]    {warning}")
    for step in plan.steps:
        print(f"[plan]    {step.method} {step.path}  # {step.name}{' (optional)' if step.optional else ''}")
        if show_bodies and step.body is not None:
            print(json.dumps(step.body, indent=2, ensure_ascii=False))


def check_url(url: str | None) -> str:
    if not url:
        raise SystemExit("no environment URL: pass --url or set VOICECRM_DATAVERSE_URL")
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise SystemExit(f"not an environment URL: {url!r}")
    if parts.netloc.lower().startswith(REFUSED_HOST_PREFIXES):
        raise SystemExit("refusing to bootstrap schema in MSX / a production Microsoft Sales environment")
    return f"{parts.scheme}://{parts.netloc}"


async def run(args: argparse.Namespace) -> int:
    profile = load_field_profile(args.profile or default_profile_path())
    topics = load_topics(args.taxonomy)

    if args.list_topic_values:
        options = profile.custom.topic_options
        topic_ids = [t for t, _ in topics]
        for topic_id, value in build_topic_option_map(topic_ids, base=options.base, overrides=options.overrides).items():
            print(f"{value}  {topic_id}")
        return 0

    plan = build_schema_plan(profile, topics)
    print_plan(plan, args.show_bodies)
    if not args.apply:
        print(f"\nDry-run: {len(plan.steps)} request(s) planned, nothing sent. Re-run with --apply to execute.")
        return 0

    org = check_url(args.url or get_settings().dataverse_url)
    if not args.yes and sys.stdin.isatty():
        if input(f"\nApply {len(plan.steps)} step(s) to {org}? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1

    token = CachedToken(None, f"{org}/.default")
    async with httpx.AsyncClient(timeout=60) as client:
        http = DataverseHttp(f"{org}/api/data/v9.2", token, client, max_retries=4, max_retry_wait=60, sleep=asyncio.sleep)
        try:
            results = await apply_plan(plan, http, apply=True)
        except DataverseError as exc:
            print(f"\nStopped: {exc}", file=sys.stderr)
            return 1
        finally:
            await token.aclose()
    failed = [r for r in results if r.status == "failed_optional"]
    print(f"\nDone: {sum(r.status == 'created' for r in results)} created, {sum(r.status == 'skipped' for r in results)} "
          f"already present, {len(failed)} optional step(s) failed.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

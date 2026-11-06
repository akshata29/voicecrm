"""VoiceCRM command line:  python -m voicecrm.cli <command>

  doctor                       show which services are live vs mock and what data is loaded
  preflight [--burst N]        validate the config and probe every live service (see docs/live-demo.md)
  teams check|subscribe|...    live Microsoft Teams ingestion: test Graph access, manage change-notification subscriptions
  crm-add --email .. --name .. --account ..   add a contact to the local CRM simulator (so a live caller resolves to an account)
  fixtures                     list the scripted demo calls
  simulate <fixture_id> [...]  run one scripted call through the real pipeline (optionally approve it)
  seed-demo                    process a representative set of calls so the inbox has something to review
  eval [--learn] [--noise]     run the golden set and print the scoreboard
  sweep                        enforce transcript/audio retention now
  serve                        start the API (and the built frontend, if present)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import uuid4

from .config import get_settings
from .container import Container, build_container
from .domain.pipeline import ApproveRequest
from .eval.harness import run_eval
from .pipeline.fixtures import ingest_from_scripted

SEED_FIXTURES = [
    "pb-teams-equity-class-c", "pb-zoom-equity-income-advisor", "pb-genesys-inbound-service",
    "retail-teams-relationship-privacy", "pb-teams-spanish-clase-c", "pb-ambiguous-equity-fund",
]


def _print_record(c: Container, rec) -> None:
    prop = rec.proposal
    print(f"\n== {rec.meta.title}  [{rec.meta.source.value} | {rec.meta.market} | {rec.meta.business_line}]")
    print(f"   status={rec.status.value}  language={rec.language}  engine={rec.engine}  template={rec.template_id}")
    if rec.error:
        print(f"   ERROR: {rec.error}")
    if not prop:
        return
    print(f"   review reasons: {prop.review.reasons or 'none'}   auto-approve eligible: {prop.review.auto_approve_eligible}")
    print("   --- note ---")
    for line in prop.note_markdown.splitlines():
        print("   " + line)
    products = c.deps.catalogue.by_id()
    for m in prop.result.products:
        chosen = products[m.chosen_product_id].display_name if m.chosen_product_id else "(needs pick)"
        alts = ", ".join(f"{cand.display_name} {cand.score:.2f}" for cand in m.candidates[:3])
        print(f"   product: '{m.surface}' -> {chosen}  conf={m.confidence:.2f}  [{alts}]")
    print(f"   topics: {[t.topic_id for t in prop.result.topics]}")
    for t in prop.result.tasks:
        print(f"   task: {t.title}  due={t.due_date}  owner={t.owner_role.value}")
    print(f"   privacy actions: {[(a.kind, a.category) for a in prop.policy_actions]}")
    print(f"   crm match: {prop.crm_match.account.name if prop.crm_match.account else None}  ambiguous={prop.crm_match.ambiguous}")
    if rec.retention:
        print(f"   retention: {rec.retention.explanation}")
    if rec.crm_result:
        print(f"   crm write: ok={rec.crm_result.ok} created={[r.entity for r in rec.crm_result.created]} "
              f"degraded={rec.crm_result.degraded_fields}")


async def _doctor(c: Container) -> int:
    print("Modes (requested -> effective):")
    for name, m in c.modes.items():
        print(f"  {name:<14} {m.requested:<14} -> {m.effective:<14} {'LIVE' if m.live else 'mock/local'}  {m.reason or ''}")
    print(f"Default engine: {c.deps.default_engine}   engines available: {c.service.engine_names()}")
    print(f"Catalogue: {len(c.deps.catalogue.products)} products (synthetic={c.deps.catalogue.synthetic}); "
          f"topics: {len(c.deps.taxonomy.topics)}; templates: {sorted(c.deps.templates)}; fixtures: {len(c.scripted_calls)}")
    print(f"CRM health: {await c.deps.crm.health()}")
    return 0


async def _simulate(c: Container, args: argparse.Namespace) -> int:
    call = c.scripted_calls.get(args.fixture_id)
    if call is None:
        print(f"unknown fixture {args.fixture_id!r}; run `fixtures` to list them", file=sys.stderr)
        return 2
    ing = ingest_from_scripted(call, via=args.via, market=args.market, template_id=args.template, asr_noise=args.noise,
                               engine=args.engine, nonce=uuid4().hex[:6], recordings_dir=c.settings.recordings_dir)
    rec = await c.service.run_to_completion(ing)
    if args.approve and rec.proposal and rec.status.value == "needs_review":
        picks = {m.mention_id: m.candidates[0].product_id for m in rec.proposal.result.products if m.chosen_product_id is None and m.candidates}
        rec = await c.service.approve(rec.meta.conversation_id, ApproveRequest(product_choices=picks, clicks=1))
    _print_record(c, rec)
    return 0 if rec.status.value != "failed" else 1


async def _seed(c: Container) -> int:
    for fid in SEED_FIXTURES:
        call = c.scripted_calls.get(fid)
        if call is None:
            continue
        rec = await c.service.run_to_completion(ingest_from_scripted(call, nonce=uuid4().hex[:6]))
        print(f"  {fid:<40} -> {rec.status.value}")
    return 0


async def _eval(c: Container, args: argparse.Namespace) -> int:
    run = await run_eval(c, engine=args.engine, with_asr_noise=args.noise, apply_learning=args.learn, label=args.label)
    c.deps.store.save_eval_run(run)
    if args.json:
        print(run.model_dump_json(indent=1))
        return 0
    m = run.metrics
    print(f"Eval {run.run_id} [{run.label}] engine={run.engine} noise={run.with_asr_noise} learned_aliases={run.learned_aliases}")
    print(f"  cases={m.cases}  product top-1={m.product_top1:.1%}  top-3={m.product_top3:.1%}  "
          f"false-tag={m.product_false_tag_rate:.1%}  share-class confusion={m.share_class_confusion_rate:.1%}")
    print(f"  topics P={m.topic_precision:.1%} R={m.topic_recall:.1%} F1={m.topic_f1:.1%}   "
          f"tasks P={m.task_precision:.1%} R={m.task_recall:.1%}")
    print(f"  privacy leaks={m.privacy_leaks}  relationship kept={m.relationship_kept_rate:.1%}  "
          f"auto-approvable={m.auto_approvable_rate:.1%}")
    print(f"  latency p50={m.latency_p50_ms}ms p95={m.latency_p95_ms}ms  mean cost/call (illustrative)=${m.mean_cost_usd}")
    print(f"\n  {'fixture':<38} {'prod 1/3/gold':<14} {'topics tp/fp/fn':<16} {'tasks tp/fp/fn':<15} leaks")
    for cs in run.cases:
        print(f"  {cs.fixture_id:<38} {cs.product_top1_correct}/{cs.product_top3_hits}/{cs.product_gold:<10} "
              f"{cs.topic_tp}/{cs.topic_fp}/{cs.topic_fn:<12} {cs.task_tp}/{cs.task_fp}/{cs.task_fn:<11} {cs.leaks}")
    return 0


def _preflight(args: argparse.Namespace) -> int:
    import json

    from .preflight import format_report, run_preflight, to_json

    settings = get_settings()
    only = {p.strip() for p in args.only.split(",") if p.strip()} or None
    checks = asyncio.run(run_preflight(settings, burst=args.burst, only=only))
    print(json.dumps(to_json(checks), indent=1) if args.json else format_report(checks, settings))
    return 1 if any(ch.status == "fail" for ch in checks) else 0


def _teams(args: argparse.Namespace) -> int:
    from .adapters.sources.teams_graph import ClientSecretAuth, GraphClient, GraphError
    from .pipeline.teams_live import TeamsIngestor
    from .preflight import Check, check_config, format_checks, probe_teams

    settings = get_settings()
    if args.teams_cmd == "check":
        checks = [c for c in check_config(settings) if c.name.startswith("config: teams")]
        if settings.teams_mode == "off":
            print("VOICECRM_TEAMS_MODE is off: set it to poll (or webhook) and the VOICECRM_TEAMS_* app settings first.")
            return 2
        if any(c.status == "fail" for c in checks):
            checks.append(Check("teams: Graph access", "fail", "the Teams settings are incomplete", "fix the lines above"))
        else:
            checks += asyncio.run(probe_teams(settings))
        print(format_checks(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0

    async def run() -> int:
        try:
            graph = GraphClient(ClientSecretAuth(tenant_id=settings.teams_tenant_id, client_id=settings.teams_client_id,
                                                 client_secret=settings.teams_client_secret))
        except ValueError as exc:
            print(exc)
            return 2
        try:
            if args.teams_cmd == "subscriptions":
                for sub in await graph.list_subscriptions():
                    print(f"{sub.get('id')}  {sub.get('resource')}  expires {sub.get('expirationDateTime')}  -> {sub.get('notificationUrl')}")
                return 0
            if args.teams_cmd == "unsubscribe":
                await graph.delete_subscription(args.subscription_id)
                print(f"deleted {args.subscription_id}")
                return 0
            base = settings.teams_public_base_url.rstrip("/")
            if not base.lower().startswith("https://") or not settings.teams_client_state:
                print("webhook mode needs VOICECRM_TEAMS_PUBLIC_BASE_URL (an https URL Graph can reach, e.g. a dev tunnel to this API) "
                      "and VOICECRM_TEAMS_CLIENT_STATE (any long random string).")
                return 2
            resource = "communications/onlineMeetings/getAllTranscripts"
            if args.organizer:
                user = await TeamsIngestor(None, graph, settings=settings)._user(args.organizer)
                resource = f"users/{user.id}/onlineMeetings/getAllTranscripts"
            sub = await graph.create_subscription(
                resource=resource, notification_url=f"{base}/api/v1/webhooks/teams", client_state=settings.teams_client_state,
                minutes=args.minutes, lifecycle_url=f"{base}/api/v1/webhooks/teams/lifecycle" if args.minutes > 60 else None)
            print(f"subscribed {sub.get('id')} to {resource}; expires {sub.get('expirationDateTime')}\n"
                  "Graph only notifies for meetings whose transcription starts AFTER this subscription exists.")
            return 0
        except GraphError as exc:
            print(f"failed: {exc}")
            return 1
        finally:
            await graph.aclose()

    return asyncio.run(run())


def _crm_add(args: argparse.Namespace) -> int:
    """Adds a contact (and its account, if new) to the local CRM simulator so a live participant resolves to an account."""
    import re

    from .adapters.crm.local_sim import LocalCrm
    from .domain.crm import CrmSeed, SeedAccount, SeedContact

    settings = get_settings()
    if settings.crm_mode != "local":
        print("crm-add only changes the local CRM simulator (VOICECRM_CRM_MODE=local).")
        return 2
    crm = LocalCrm(settings.crm_db_path)
    try:
        wanted = args.account.strip().lower()
        account = next((r for r in crm._query("SELECT * FROM accounts") if wanted in (r["id"].lower(), r["name"].lower())), None)
        new_account = account is None
        account_id = account["id"] if account else "acct-" + re.sub(r"[^a-z0-9]+", "-", wanted).strip("-")
        accounts = [SeedAccount(id=account_id, name=args.account.strip(), domain=args.email.split("@")[-1])] if new_account else []
        contact = SeedContact(id="contact-" + re.sub(r"[^a-z0-9]+", "-", args.email.lower()).strip("-"), name=args.name,
                              email=args.email, phone=args.phone, account_id=account_id, title=args.title)
        crm.load_seed(CrmSeed(users=[], accounts=accounts, contacts=[contact], opportunities=[]))
        print(f"{'created account ' + args.account + ' and ' if new_account else ''}added {args.name} <{args.email}> "
              f"to {account['name'] if account else args.account} in the local CRM simulator")
        return 0
    finally:
        crm.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voicecrm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("fixtures")
    sim = sub.add_parser("simulate")
    sim.add_argument("fixture_id")
    sim.add_argument("--via", choices=["transcript", "audio"], default="transcript")
    sim.add_argument("--market")
    sim.add_argument("--template")
    sim.add_argument("--engine", choices=["rules", "llm", "hybrid"])
    sim.add_argument("--noise", action="store_true", help="inject ASR noise")
    sim.add_argument("--approve", action="store_true", help="approve (picking the top candidate if needed) and write to the CRM")
    sub.add_parser("seed-demo")
    ev = sub.add_parser("eval")
    ev.add_argument("--engine", choices=["rules", "llm", "hybrid"])
    ev.add_argument("--noise", action="store_true")
    ev.add_argument("--learn", action="store_true", help="apply (simulated) reviewer corrections before measuring")
    ev.add_argument("--label")
    ev.add_argument("--json", action="store_true")
    sub.add_parser("sweep")
    pre = sub.add_parser("preflight", help="validate the configuration and probe every live service (synthetic data only)")
    pre.add_argument("--burst", type=int, default=0, metavar="N", help="also fire N concurrent calls at each live service")
    pre.add_argument("--only", default="", help="comma-separated subset of: speech, language, model, search, vault, crm, teams")
    pre.add_argument("--json", action="store_true")
    tm = sub.add_parser("teams", help="live Teams ingestion: check Graph access, manage change-notification subscriptions")
    tsub = tm.add_subparsers(dest="teams_cmd", required=True)
    tsub.add_parser("check", help="sign in to Graph as the app and test everything the ingestion needs")
    tsub.add_parser("subscriptions", help="list this app's Graph subscriptions")
    tsb = tsub.add_parser("subscribe", help="webhook mode: ask Graph to POST new transcripts to VOICECRM_TEAMS_PUBLIC_BASE_URL")
    tsb.add_argument("--organizer", help="only this organiser's meetings (UPN or object id); default: the whole tenant")
    tsb.add_argument("--minutes", type=int, default=55, help="subscription lifetime (over 60 also needs the lifecycle endpoint)")
    tun = tsub.add_parser("unsubscribe", help="delete a Graph subscription")
    tun.add_argument("subscription_id")
    ca = sub.add_parser("crm-add", help="add a contact to the local CRM simulator so a live participant resolves to an account")
    ca.add_argument("--email", required=True)
    ca.add_argument("--name", required=True)
    ca.add_argument("--account", required=True, help="an existing account (id or name) or a new account name")
    ca.add_argument("--phone")
    ca.add_argument("--title")
    srv = sub.add_parser("serve")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("voicecrm.api.main:app", host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "preflight":
        return _preflight(args)
    if args.cmd == "teams":
        return _teams(args)
    if args.cmd == "crm-add":
        return _crm_add(args)

    settings = get_settings()
    container = build_container(settings)

    async def run() -> int:
        if args.cmd == "doctor":
            return await _doctor(container)
        if args.cmd == "fixtures":
            for f in container.scripted_calls.values():
                print(f"{f.fixture_id:<40} {f.channel.value:<8} {f.language:<6} {f.market:<3} {f.business_line:<18} {','.join(f.tags)}")
            return 0
        if args.cmd == "simulate":
            return await _simulate(container, args)
        if args.cmd == "seed-demo":
            return await _seed(container)
        if args.cmd == "eval":
            return await _eval(container, args)
        if args.cmd == "sweep":
            items = await container.service.sweep_retention()
            print(f"deleted {len(items)} expired vault item(s)")
            return 0
        return 2

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())

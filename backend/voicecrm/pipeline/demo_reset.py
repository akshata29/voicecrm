"""Reset the demo: remove what earlier runs stored, so the next one starts from an empty Inbox.

Removed: every conversation (with its stage events, usage and review feedback), every vault copy, what the pipeline wrote to
the local CRM simulator (phone calls, tasks, insights), learned aliases, evaluation runs, and the policy changes saved from the
Policies page. The in-memory figures that describe those calls (pipeline throughput, the last load-test report, the Teams
status card, the phone line's finished calls) are cleared with them.

Kept: the catalogue, taxonomy and templates; the CRM seed (accounts, contacts and opportunities, including contacts added with
`crm-add`); the recordings under data/audio; .env; and which Teams meetings and phone events were already handled, so a reset
does not bring them back.

Safeguards: it only runs against the local CRM simulator (a real Dataverse is never touched), never while calls are being
processed or a load test is running, and the API needs an explicit `confirm`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..domain.base import VModel
from .service import PipelineError

if TYPE_CHECKING:
    from ..container import Container

logger = logging.getLogger(__name__)

_VAULT_PARALLEL = 16


class DemoCounts(VModel):
    """What a reset would remove (GET /demo/data) or has removed (POST /demo/reset)."""

    conversations: int = 0
    crm_phone_calls: int = 0
    crm_tasks: int = 0
    crm_insights: int = 0
    vault_items: int = 0
    learned_aliases: int = 0
    evaluation_runs: int = 0
    policy_changes: bool = False


class DemoData(DemoCounts):
    can_reset: bool
    reason: str | None = None            # why a reset is not possible right now
    in_flight: int = 0
    crm: str = ""


class DemoResetRequest(VModel):
    confirm: bool = False


def blocked_reason(c: Container) -> str | None:
    """Why a reset cannot run right now, or None when it can."""
    if not callable(getattr(c.deps.crm, "clear_written", None)):
        return ("The CRM is a real Dataverse environment. A reset only works with the local CRM simulator "
                "(VOICECRM_CRM_MODE=local), so a real CRM is never touched.")
    if c.load.active:
        return "A load test is running: stop it, or wait for it to finish."
    busy = c.service.in_flight
    if busy:
        return f"{busy} call{' is' if busy == 1 else 's are'} still being processed: wait for {'it' if busy == 1 else 'them'} to finish."
    return None


async def inspect(c: Container) -> DemoData:
    """What a reset would remove, and whether it can run now."""
    stored = c.deps.store.count_all()
    crm = (await c.deps.crm.health()).get("counts") or {}
    items = await c.deps.vault.list_items()
    reason = blocked_reason(c)
    return DemoData(
        conversations=stored["conversations"], crm_phone_calls=int(crm.get("phonecalls", 0)), crm_tasks=int(crm.get("tasks", 0)),
        crm_insights=int(crm.get("insights", 0)), vault_items=len(items), learned_aliases=stored["aliases"],
        evaluation_runs=stored["eval_runs"], policy_changes=c.policy_store.customised, can_reset=reason is None,
        reason=reason, in_flight=c.service.in_flight, crm=getattr(c.deps.crm, "name", ""))


async def reset(c: Container) -> DemoCounts:
    """Remove everything listed in the module docstring. Returns what was removed."""
    reason = blocked_reason(c)
    if reason:
        raise PipelineError(reason, status=409)

    items = await c.deps.vault.list_items()
    ids = sorted({item.conversation_id for item in items})
    gate = asyncio.Semaphore(_VAULT_PARALLEL)

    async def purge(conversation_id: str) -> None:
        async with gate:
            await c.deps.vault.delete(conversation_id)

    await asyncio.gather(*(purge(conversation_id) for conversation_id in ids))
    crm = await c.deps.crm.clear_written()
    stored = c.deps.store.clear_all()
    policies = c.policy_store.reset()
    c.service.reload_aliases()
    c.service.stats.reset()
    c.load.forget()
    if c.teams is not None:
        c.teams.forget()
    if c.phone is not None:
        c.phone.forget()

    removed = DemoCounts(
        conversations=stored["conversations"], crm_phone_calls=crm.get("phonecalls", 0), crm_tasks=crm.get("tasks", 0),
        crm_insights=crm.get("insights", 0), vault_items=len(items), learned_aliases=stored["aliases"],
        evaluation_runs=stored["eval_runs"], policy_changes=policies)
    logger.info("demo reset: %s", removed.model_dump())
    return removed

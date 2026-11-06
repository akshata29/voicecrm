"""Health and configuration."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import __version__
from ...container import Container
from ...domain.enums import Channel
from ..deps import get_container
from ..schemas import ConfigResponse, TemplateInfo

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config", response_model=ConfigResponse)
async def config(c: Container = Depends(get_container)) -> ConfigResponse:
    policies = c.policy_store.bundle
    templates = [
        TemplateInfo(template_id=t.template_id, name=t.name, description=t.description, business_lines=t.business_lines,
                     call_types=t.call_types, sections=[s.title for s in t.sections])
        for t in c.deps.templates.values()
    ]
    return ConfigResponse(
        app_version=__version__, demo_mode=c.settings.demo_mode, modes=c.modes, markets=list(policies.markets.values()),
        templates=templates, business_lines=sorted({b for t in c.deps.templates.values() for b in t.business_lines}),
        channels=[ch.value for ch in Channel], engine=c.deps.default_engine,
        catalogue_products=len(c.deps.catalogue.products), catalogue_synthetic=c.deps.catalogue.synthetic,
        pricing_illustrative=c.deps.pricing.illustrative,
        llm_deployment=c.settings.foundry_deployment if c.modes["llm"].live and c.settings.foundry_deployment else None)

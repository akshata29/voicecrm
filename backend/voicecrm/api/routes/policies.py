"""Policies, templates, taxonomy, catalogue, learned aliases, retention preview."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from ...container import Container
from ...domain.catalogue import LearnedAlias
from ...domain.policy import MarketPolicy, NotesPolicy, ProcessingPolicy
from ...domain.taxonomy import Taxonomy
from ...domain.templates import SummaryTemplate
from ..deps import get_container
from ..schemas import CatalogueStats, PoliciesResponse, ProductSearchHit, RetentionPreview

router = APIRouter()


def _policies(c: Container) -> PoliciesResponse:
    b = c.policy_store.bundle
    return PoliciesResponse(markets=list(b.markets.values()), notes=b.notes, processing=b.processing)


@router.get("/policies", response_model=PoliciesResponse)
async def get_policies(c: Container = Depends(get_container)) -> PoliciesResponse:
    return _policies(c)


@router.put("/policies/markets/{market}", response_model=PoliciesResponse)
async def put_market(market: str, body: MarketPolicy, c: Container = Depends(get_container)) -> PoliciesResponse:
    if body.market != market:
        raise HTTPException(422, "market in path and body differ")
    if body.transcript_retention == "on" and not body.transcript_retention_days:
        raise HTTPException(422, "transcript_retention_days is required when transcript retention is on")
    c.policy_store.update_market(body)
    return _policies(c)


@router.put("/policies/processing", response_model=PoliciesResponse)
async def put_processing(body: ProcessingPolicy, c: Container = Depends(get_container)) -> PoliciesResponse:
    if body.engine not in c.service.engine_names():
        raise HTTPException(422, f"engine '{body.engine}' is not available (available: {c.service.engine_names()})")
    c.policy_store.update_processing(body)
    return _policies(c)


@router.put("/policies/notes", response_model=PoliciesResponse)
async def put_notes(body: NotesPolicy, c: Container = Depends(get_container)) -> PoliciesResponse:
    c.policy_store.update_notes(body)
    return _policies(c)


@router.get("/policies/retention-preview", response_model=RetentionPreview)
async def retention_preview(market: str, c: Container = Depends(get_container)) -> RetentionPreview:
    return RetentionPreview(market=market, decision=c.service.retention_preview(market))


@router.get("/templates", response_model=list[SummaryTemplate])
async def templates(c: Container = Depends(get_container)) -> list[SummaryTemplate]:
    return list(c.deps.templates.values())


@router.get("/taxonomy", response_model=Taxonomy)
async def taxonomy(c: Container = Depends(get_container)) -> Taxonomy:
    return c.deps.taxonomy


@router.get("/catalogue/stats", response_model=CatalogueStats)
async def catalogue_stats(c: Container = Depends(get_container)) -> CatalogueStats:
    cat = c.deps.catalogue
    return CatalogueStats(
        products=len(cat.products), families=len({p.family_id for p in cat.products}),
        share_classes=dict(Counter(p.share_class for p in cat.products)),
        asset_classes=dict(Counter(p.asset_class for p in cat.products)),
        synthetic=cat.synthetic, note=cat.note, learned_aliases=len(c.deps.store.list_aliases()))


@router.get("/catalogue/search", response_model=list[ProductSearchHit])
async def catalogue_search(q: str = Query(..., min_length=1), top: int = Query(10, le=50),
                           c: Container = Depends(get_container)) -> list[ProductSearchHit]:
    products = c.deps.catalogue.by_id()
    hits = await c.search.search(q, top=top)
    return [
        ProductSearchHit(product_id=h.id, display_name=products[h.id].display_name, fund_name=products[h.id].fund_name,
                         share_class=products[h.id].share_class, asset_class=products[h.id].asset_class,
                         score=round(h.score, 4))
        for h in hits if h.id in products
    ]


@router.get("/aliases", response_model=list[LearnedAlias])
async def aliases(c: Container = Depends(get_container)) -> list[LearnedAlias]:
    return c.deps.store.list_aliases()


@router.delete("/aliases")
async def delete_aliases(c: Container = Depends(get_container)) -> dict[str, int]:
    n = c.deps.store.clear_aliases()
    c.service.reload_aliases()
    return {"deleted": n}

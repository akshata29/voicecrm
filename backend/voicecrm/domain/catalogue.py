"""Product catalogue (synthetic, illustrative) and learned aliases."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .base import VModel
from .conversation import utcnow


class Product(VModel):
    product_id: str                            # synthetic, e.g. "SYN-000123" (never a real identifier)
    family_id: str                             # same fund, all share classes share this
    fund_name: str                             # "Contoso Equity Fund"
    share_class: str                           # canonical class key: "A", "C", "Advisor", "R6", "I", ...
    display_name: str                          # "Contoso Equity Fund Class C"
    asset_class: str                           # Equity | Fixed Income | Multi-Asset | Alternatives | ...
    strategy: str
    region: str                                # US | Global | Emerging Markets | Europe | ...
    vehicle: str = "Mutual Fund"               # Mutual Fund | ETF | UCITS | SMA
    business_lines: list[str] = Field(default_factory=list)   # areas where the product is relevant
    aliases: list[str] = Field(default_factory=list)          # alternative written/spoken fund names (EN + ES)
    status: str = "active"
    synthetic: bool = True


class Catalogue(VModel):
    version: str
    synthetic: bool = True
    note: str = "SYNTHETIC illustrative catalogue - not any real firm's product list."
    # canonical share class -> spoken/written variants (lower-case), EN + ES. Used by detectors.
    share_classes: dict[str, list[str]] = Field(default_factory=dict)
    products: list[Product]

    def by_id(self) -> dict[str, Product]:
        return {p.product_id: p for p in self.products}


class LearnedAlias(VModel):
    """Created when a reviewer corrects a product; applied on later runs (the 'improves over time' loop)."""

    surface: str                               # normalised surface form as heard, e.g. "equity fund c shares"
    product_id: str
    count: int = 1
    source: str = "reviewer_correction"
    created_at: datetime = Field(default_factory=utcnow)

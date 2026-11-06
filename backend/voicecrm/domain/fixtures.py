"""Scripted demo calls with gold labels. Source of the golden set, the demo fixtures and the offline mock transcriber."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from .base import VModel
from .enums import Channel, Direction, MentionType, OwnerRole
from .conversation import Participant


class ScriptedTurn(VModel):
    speaker: Literal["seller", "customer", "internal"]
    speaker_id: str | None = None              # participant id; defaults: seller->"seller", customer->"cust1", internal->"int1"
    text: str
    locale: str = "en-US"                      # per-turn language (Spanish turns use es-US)


class GoldProduct(VModel):
    product_id: str                            # exact catalogue product incl. share class
    surface_forms: list[str] = Field(default_factory=list)   # how it is said in the script
    mention_type: MentionType = MentionType.interest
    ambiguous: bool = False                    # True when the script never states the share class (top-3 pick expected)
    acceptable_ids: list[str] = Field(default_factory=list)  # for ambiguous mentions: ids that count as a correct top-3 hit


class GoldTask(VModel):
    keywords: list[str]                        # all must appear in the task title/description (lower-case)
    owner_role: OwnerRole = OwnerRole.seller
    due_date: date | None = None               # resolved against the call date; None = no date stated


class GoldLabels(VModel):
    template_id: str
    expected_language: str = "en"
    products: list[GoldProduct] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)          # topic_ids
    tasks: list[GoldTask] = Field(default_factory=list)
    must_not_leak: list[str] = Field(default_factory=list)   # literal strings that must NOT appear in any CRM field
    must_keep: list[str] = Field(default_factory=list)       # lower-case keywords of relationship facts that SHOULD be kept
    expect_crm_account: str | None = None                    # SeedAccount id the call should match
    notes: str | None = None


class ScriptedCall(VModel):
    fixture_id: str                            # e.g. "pb-teams-equity-class-c"
    title: str
    description: str                           # what this call demonstrates
    channel: Channel
    direction: Direction = Direction.outbound
    market: str = "US"
    business_line: str = "professional_buyer"
    call_type: str | None = None
    language: str = "en-US"
    started_at: str = "2026-11-10T15:00:00Z"   # ISO-8601 UTC; also the anchor for relative dates ("by Friday")
    duration_minutes: int = 12
    seller: Participant
    customers: list[Participant]
    internal: list[Participant] = Field(default_factory=list)   # seller-side colleagues who speak (id "int1", ...)
    customer_org: str | None = None
    turns: list[ScriptedTurn]
    gold: GoldLabels
    tags: list[str] = Field(default_factory=list)            # e.g. ["hero", "privacy", "spanish", "ambiguous_product"]

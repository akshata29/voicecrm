"""CRM-facing models. The transcript is NEVER part of a CrmPackage (requirement RET-2)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from .base import VModel
from .enums import Channel
from .extraction import NoteSection


class CrmRecordRef(VModel):
    entity: str                                # account | contact | opportunity | phonecall | task | insight | systemuser
    id: str
    name: str | None = None


class CrmMatch(VModel):
    account: CrmRecordRef | None = None
    contact: CrmRecordRef | None = None
    opportunity: CrmRecordRef | None = None
    confidence: float = 0.0
    method: str = "none"                       # email | phone | name_org | meeting_metadata | manual | none
    ambiguous: bool = False
    candidates: list[CrmRecordRef] = Field(default_factory=list)


class PhoneCallPayload(VModel):
    subject: str
    description: str                           # rendered note (markdown/plain)
    direction: Literal["inbound", "outbound"] = "outbound"
    start: datetime
    end: datetime | None = None
    duration_minutes: int | None = None
    regarding: CrmRecordRef | None = None
    to_contact: CrmRecordRef | None = None
    owner_email: str | None = None


class TaskPayload(VModel):
    source_task_id: str                        # ExtractionResult task_id, for idempotency
    subject: str
    description: str | None = None
    due: date | None = None
    regarding: CrmRecordRef | None = None
    owner_email: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"


class InsightPayload(VModel):
    """Structured, minable record of the conversation (what a downstream analytics team consumes)."""

    source: Channel
    source_interaction_id: str
    template_id: str
    language: str
    summary_markdown: str
    sections: list[NoteSection]
    product_ids: list[str]
    topic_ids: list[str]
    relationship_facts: list[str] = Field(default_factory=list)
    engine: str
    model: str | None = None
    confidence: float
    review_status: Literal["approved", "auto_approved"]
    transcript_pointer: str | None = None      # pointer into the vault when a transcript is retained; never the text


class CrmPackage(VModel):
    idempotency_key: str                       # f"{source}:{source_interaction_id}:v{n}"
    conversation_id: str
    regarding: CrmMatch
    phone_call: PhoneCallPayload
    tasks: list[TaskPayload] = Field(default_factory=list)
    insight: InsightPayload


class CrmWriteResult(VModel):
    ok: bool
    created: list[CrmRecordRef] = Field(default_factory=list)
    skipped_duplicate: bool = False
    degraded_fields: list[str] = Field(default_factory=list)   # fields written in fallback form (e.g. custom column missing)
    error: str | None = None


# ---- seed data for the local CRM simulator (data/crm/seed.json) -------------------------------------
class SeedAccount(VModel):
    id: str
    name: str
    domain: str | None = None
    segment: str = "professional_buyer"        # business line


class SeedContact(VModel):
    id: str
    name: str
    email: str | None = None
    phone: str | None = None
    account_id: str
    title: str | None = None


class SeedOpportunity(VModel):
    id: str
    name: str
    account_id: str
    contact_id: str | None = None
    stage: str = "Qualify"


class SeedUser(VModel):
    id: str
    name: str
    email: str


class CrmSeed(VModel):
    synthetic: bool = True
    users: list[SeedUser]
    accounts: list[SeedAccount]
    contacts: list[SeedContact]
    opportunities: list[SeedOpportunity]

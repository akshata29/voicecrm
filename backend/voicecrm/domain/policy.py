"""Policy models: per-market retention, notes policy (what may enter CRM), processing policy."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .base import VModel


class MarketPolicy(VModel):
    market: str                                # key, e.g. "US", "UK", "EU"
    display_name: str
    transcript_retention: Literal["on", "off"]
    transcript_retention_days: int | None = None       # required when retention is "on"
    store_redacted_only: bool = True                   # keep only the PII-redacted transcript
    audio_retention_days: int = 0                      # 0 = delete audio right after transcription
    summary_retention_days: int | None = None          # None = as long as the CRM record exists
    notes: str | None = None                           # example policy; not any real firm's actual policy

    @field_validator("transcript_retention", mode="before")
    @classmethod
    def _coerce_yaml_bool(cls, v):
        # YAML 1.1 parses unquoted on/off as booleans; accept them.
        if v is True:
            return "on"
        if v is False:
            return "off"
        return v


class NotesPolicy(VModel):
    # categories that must never reach a CRM note:
    omit_categories: list[str] = Field(default_factory=lambda: [
        "payment_card", "bank_account", "government_id", "credentials", "medical", "family_health",
        "personal_contact",
    ])
    keep_relationship_context: bool = True             # keep non-sensitive rapport facts ("Seattle fan")
    drop_small_talk: bool = True


class ProcessingPolicy(VModel):
    engine: Literal["rules", "llm", "hybrid"] = "rules"
    auto_approve_enabled: bool = False
    auto_approve_min_confidence: float = 0.9
    auto_approve_requires_crm_match: bool = True
    product_review_threshold: float = 0.75             # below this a product mention needs a human pick
    top_k_products: int = 3
    # channel -> ordered transcript sources: "platform" (the source's own transcript, if entitled/free) or "azure_speech"
    transcription_source_preference: dict[str, list[str]] = Field(default_factory=dict)
    default_template_by_business_line: dict[str, str] = Field(default_factory=dict)


class PolicyBundle(VModel):
    markets: dict[str, MarketPolicy]
    notes: NotesPolicy = Field(default_factory=NotesPolicy)
    processing: ProcessingPolicy = Field(default_factory=ProcessingPolicy)

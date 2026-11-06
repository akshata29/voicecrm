"""Usage metering and (illustrative) cost model. Price tables must be validated against current Azure pricing."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import VModel
from .conversation import utcnow
from .enums import Stage


class UsageRecord(VModel):
    conversation_id: str
    stage: Stage
    service: Literal["speech", "llm", "language_pii", "search", "embeddings"]
    provider: str                              # e.g. "azure_speech", "foundry", "mock", "local"
    model: str | None = None
    audio_seconds: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    units: float | None = None                 # generic unit (text records, queries)
    latency_ms: int | None = None
    ts: datetime = Field(default_factory=utcnow)


class LlmPrice(VModel):
    input_per_1m_tokens: float
    output_per_1m_tokens: float


class PricingTable(VModel):
    illustrative: bool = True                  # True until validated against the current Azure price list
    currency: str = "USD"
    note: str = "Illustrative placeholders - validate against current Azure pricing before quoting."
    speech_per_audio_hour: float = 0.0
    llm: dict[str, LlmPrice] = Field(default_factory=dict)             # model/deployment -> prices; "default" is the fallback
    language_pii_per_1k_records: float = 0.0
    search_per_1k_queries: float = 0.0


class CostLine(VModel):
    service: str
    quantity: float
    unit: str
    est_cost: float


class CostBreakdown(VModel):
    lines: list[CostLine] = Field(default_factory=list)
    total: float = 0.0
    currency: str = "USD"
    illustrative: bool = True

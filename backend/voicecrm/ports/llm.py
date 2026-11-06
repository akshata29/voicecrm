"""LLM port (structured JSON output). Adapters: fake (tests), Microsoft Foundry / Azure OpenAI."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.base import VModel


class LlmUsage(VModel):
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cached_tokens: int | None = None


class LlmResult(VModel):
    data: dict[str, Any]                       # parsed JSON that conforms to the supplied schema
    usage: LlmUsage


@runtime_checkable
class LlmPort(Protocol):
    name: str
    model: str

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> LlmResult: ...

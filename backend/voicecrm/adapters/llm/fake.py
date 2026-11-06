"""Deterministic LLM double for tests: queued canned JSON, estimated token usage and a log of every call."""
from __future__ import annotations

import copy
import inspect
import json
import math
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ...ports.llm import LlmPort, LlmResult, LlmUsage
from .errors import LlmOutputError


@dataclass(frozen=True)
class LlmCall:
    """One recorded ``generate_json`` invocation (what the engine asked the model)."""

    system: str
    user: str
    json_schema: dict[str, Any]
    schema_name: str
    max_output_tokens: int
    temperature: float


Responder = Callable[[LlmCall], "dict[str, Any] | Exception | Awaitable[dict[str, Any] | Exception]"]


class FakeLlm(LlmPort):
    """Returns canned dicts in order, or whatever a callable computes from the recorded call.

    ``responses`` is an iterable of dicts (an ``Exception`` instance in the queue is raised instead) or a callable
    ``(LlmCall) -> dict`` (sync or async). An empty queue raises ``LlmOutputError`` unless ``repeat_last`` is set.
    Token usage is estimated at ``chars_per_token`` characters per token.
    """

    name = "fake"

    def __init__(
        self,
        responses: Iterable[dict[str, Any] | Exception] | Responder | None = None,
        *,
        model: str = "fake-llm",
        chars_per_token: int = 4,
        latency_ms: int = 0,
        repeat_last: bool = False,
    ) -> None:
        self.model = model
        self.calls: list[LlmCall] = []
        self._chars_per_token = max(1, chars_per_token)
        self._latency_ms = latency_ms
        self._repeat_last = repeat_last
        self._last: dict[str, Any] | Exception | None = None
        self._responder: Responder | None = None
        self._queue: deque[dict[str, Any] | Exception] = deque()
        if callable(responses):
            self._responder = responses
        elif isinstance(responses, dict):
            self._queue.append(responses)
        elif responses is not None:
            self._queue.extend(responses)

    def enqueue(self, *responses: dict[str, Any] | Exception) -> None:
        self._queue.extend(responses)

    @property
    def last_call(self) -> LlmCall | None:
        return self.calls[-1] if self.calls else None

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> LlmResult:
        call = LlmCall(system, user, copy.deepcopy(json_schema), schema_name, max_output_tokens, temperature)
        self.calls.append(call)
        outcome = await self._next(call)
        if isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, dict):
            raise LlmOutputError(f"FakeLlm canned response must be a dict, got {type(outcome).__name__}")
        data = copy.deepcopy(outcome)
        prompt_chars = len(system) + len(user) + len(json.dumps(json_schema, default=str))
        return LlmResult(
            data=data,
            usage=LlmUsage(
                model=self.model,
                tokens_in=self._tokens(prompt_chars),
                tokens_out=self._tokens(len(json.dumps(data, default=str))),
                latency_ms=self._latency_ms,
                cached_tokens=None,
            ),
        )

    async def _next(self, call: LlmCall) -> Any:
        if self._responder is not None:
            outcome = self._responder(call)
            return await outcome if inspect.isawaitable(outcome) else outcome
        if self._queue:
            self._last = self._queue.popleft()
        elif not (self._repeat_last and self._last is not None):
            raise LlmOutputError(f"FakeLlm has no canned response left for call #{len(self.calls)}")
        return self._last

    def _tokens(self, chars: int) -> int:
        return max(1, math.ceil(chars / self._chars_per_token))

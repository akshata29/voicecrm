"""Microsoft Foundry / Azure OpenAI model deployment behind ``LlmPort`` (openai SDK, v1 API, strict JSON schema).

The SDK's ``AsyncOpenAI`` client is pointed at the resource's v1 endpoint, ``https://<resource>.openai.azure.com/openai/v1/``
(``*.services.ai.azure.com`` works too): no ``api-version`` is needed and the Azure deployment name goes in ``model``.
Auth is Entra (token provider -> ``Authorization: Bearer``; scope ``https://ai.azure.com/.default``) or a key
(``api-key``). Calls use Chat Completions with ``response_format={"type": "json_schema", ...strict}``.

Robustness: a model that rejects ``temperature`` / ``reasoning_effort`` is retried without it (and remembered);
``max_completion_tokens`` vs ``max_tokens`` is negotiated the same way; 408/429/5xx and connection errors are retried
(six times by default, exponential back-off from 1 s to 30 s with Retry-After as a floor, about a minute in all) because a
429 under a bulk run means "slow down", not "failed"; unusable JSON gets exactly one repair retry, then ``LlmOutputError``.
Token usage is summed across attempts. Prompts, transcripts and model output are never logged (not even at DEBUG).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Self

import openai
from openai import AsyncOpenAI

from ...ports.llm import LlmPort, LlmResult, LlmUsage
from ..transcription._azure_common import AzureAuth, Sleep, backoff_delay, parse_retry_after
from .errors import LlmOutputError, LlmRequestError

logger = logging.getLogger(__name__)

FOUNDRY_SCOPE = "https://ai.azure.com/.default"

# JSON Schema keywords Azure OpenAI strict structured outputs reject (docs: "Unsupported type-specific keywords").
_UNSUPPORTED_KEYWORDS = (
    "minLength", "maxLength", "pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "patternProperties", "unevaluatedProperties", "propertyNames", "minProperties", "maxProperties",
    "unevaluatedItems", "contains", "minContains", "maxContains", "minItems", "maxItems", "uniqueItems", "default",
    "examples",
)


def ensure_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Returns a copy of ``schema`` that satisfies OpenAI/Azure strict structured outputs.

    Every object gets ``additionalProperties: false`` and lists all its properties as ``required``; properties that
    were optional become nullable (``null`` stands for "absent"), so consumers must tolerate ``None`` there. Keywords
    the strict subset rejects (``minLength``, ``pattern``, ``format``, ``minItems`` ...) and ``default`` are dropped
    at schema level (a *property* called ``format`` is untouched) and ``oneOf`` becomes ``anyOf``.
    """
    out = copy.deepcopy(schema)
    _strictify(out)
    return out


def _strictify(schema: dict[str, Any]) -> None:
    for keyword in _UNSUPPORTED_KEYWORDS:
        schema.pop(keyword, None)
    if "oneOf" in schema:
        schema["anyOf"] = schema.pop("oneOf")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        originally_required = set(schema.get("required") or [])
        for name, sub in properties.items():
            if isinstance(sub, dict):
                _strictify(sub)
                if name not in originally_required:
                    _make_nullable(sub)
        schema["required"] = list(properties)
        schema["additionalProperties"] = False
    if isinstance(schema.get("items"), dict):
        _strictify(schema["items"])
    for key in ("anyOf", "allOf"):
        for sub in schema.get(key) or []:
            if isinstance(sub, dict):
                _strictify(sub)
    for key in ("$defs", "definitions"):
        for sub in (schema.get(key) or {}).values():
            if isinstance(sub, dict):
                _strictify(sub)


def _make_nullable(schema: dict[str, Any]) -> None:
    if "anyOf" in schema:
        if not any(isinstance(s, dict) and s.get("type") == "null" for s in schema["anyOf"]):
            schema["anyOf"].append({"type": "null"})
    elif "$ref" in schema:
        schema["anyOf"] = [{"$ref": schema.pop("$ref")}, {"type": "null"}]
    elif isinstance(schema.get("type"), str):
        if schema["type"] != "null":
            schema["type"] = [schema["type"], "null"]
        if isinstance(schema.get("enum"), list) and None not in schema["enum"]:
            schema["enum"] = [*schema["enum"], None]
    elif isinstance(schema.get("type"), list) and "null" not in schema["type"]:
        schema["type"] = [*schema["type"], "null"]


@dataclass
class _Totals:
    tokens_in: int = 0
    tokens_out: int = 0
    cached: int | None = None

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is not None:
            self.cached = (self.cached or 0) + int(cached)


class FoundryLlm(LlmPort):
    name = "foundry"

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        key: str | None = None,
        auth: Literal["entra", "key"] = "entra",
        credential: Any | None = None,
        token_scope: str = FOUNDRY_SCOPE,
        http_client: Any | None = None,
        client: AsyncOpenAI | None = None,
        timeout: float = 120.0,
        max_retries: int = 6,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        jitter: float = 0.1,
        max_wait: float = 120.0,
        sleep: Sleep = asyncio.sleep,
        strict_schema: bool = True,
        token_limit_param: Literal["auto", "max_completion_tokens", "max_tokens"] = "auto",
        reasoning_effort: str | None = None,
        repair_echo_chars: int = 4000,
    ) -> None:
        """``endpoint`` is the resource URL (``/openai/v1`` is appended); ``deployment`` is the model deployment name.

        ``http_client`` / ``client`` inject an httpx client or a ready ``AsyncOpenAI`` (tests); injected objects are
        not closed by ``aclose``. ``strict_schema`` normalises schemas with ``ensure_strict_schema`` before sending.
        """
        if not endpoint or not deployment:
            raise ValueError("FoundryLlm needs the resource endpoint and the model deployment name")
        self.model = deployment
        self._auth = AzureAuth(service="Foundry", mode=auth, key=key, scope=token_scope, key_header="api-key",
                               credential=credential)
        self._scope = token_scope
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._jitter = jitter
        self._max_wait = max_wait
        self._sleep = sleep
        self._strict = strict_schema
        self._limit_auto = token_limit_param == "auto"
        self._limit_param = "max_completion_tokens" if self._limit_auto else token_limit_param
        self._reasoning_effort = reasoning_effort
        self._repair_echo_chars = repair_echo_chars
        self._dropped: set[str] = set()                      # parameters this deployment rejected, never sent again
        if client is not None:
            self._client, self._owns_client = client, False
        else:
            options: dict[str, Any] = {"base_url": _v1_base_url(endpoint), "max_retries": 0, "timeout": timeout,
                                       "http_client": http_client}
            if auth == "key":
                options.update(api_key=key, default_headers={"api-key": str(key)})
            else:
                options["api_key"] = self._auth.token         # async callable: refreshed per request
            self._client = AsyncOpenAI(**options)
            self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()
        await self._auth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- LlmPort ----------------------------------------------------------------------------------------------
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
        started = time.perf_counter()
        schema = ensure_strict_schema(json_schema) if self._strict else copy.deepcopy(json_schema)
        response_format = {"type": "json_schema",
                           "json_schema": {"name": _schema_name(schema_name), "strict": self._strict, "schema": schema}}
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        totals = _Totals()
        for attempt in (1, 2):                                # attempt 2 is the single JSON repair retry
            completion = await self._complete(messages, response_format, max_output_tokens, temperature, totals)
            choice = completion.choices[0] if completion.choices else None
            if choice is None:
                raise LlmOutputError("the model returned no choices", attempts=attempt)
            finish = choice.finish_reason
            if getattr(choice.message, "refusal", None):
                raise LlmOutputError("the model refused to answer the request", finish_reason=finish, attempts=attempt)
            if finish == "content_filter":
                raise LlmOutputError("the response was blocked by the content filter", finish_reason=finish,
                                     attempts=attempt)
            content = choice.message.content
            data, problem = _parse_json_object(content, json_schema)
            if data is not None:
                latency_ms = round((time.perf_counter() - started) * 1000)
                logger.info("foundry call ok: deployment=%s tokens_in=%d tokens_out=%d latency_ms=%d attempts=%d",
                            self.model, totals.tokens_in, totals.tokens_out, latency_ms, attempt)
                return LlmResult(data=data, usage=LlmUsage(
                    model=self.model, tokens_in=totals.tokens_in, tokens_out=totals.tokens_out, latency_ms=latency_ms,
                    cached_tokens=totals.cached))
            if attempt == 2:
                truncated = finish == "length"
                raise LlmOutputError(
                    f"the model did not return usable JSON after one repair retry ({problem})"
                    + (f"; output was cut off at max_output_tokens={max_output_tokens} - raise the limit" if truncated
                       else ""),
                    finish_reason=finish, attempts=attempt)
            logger.warning("foundry output was not usable JSON (%s, finish_reason=%s); one repair retry", problem, finish)
            messages = self._repair_messages(messages, content, problem or "invalid JSON", finish)
        raise AssertionError("unreachable")                   # pragma: no cover

    # ---- one HTTP round trip with the retry / negotiation rules ------------------------------------------------
    async def _complete(
        self, messages: list[dict[str, Any]], response_format: dict[str, Any], max_output_tokens: int,
        temperature: float, totals: _Totals,
    ) -> Any:
        transient = 0
        adaptations = 0
        while True:
            params: dict[str, Any] = {"model": self.model, "messages": messages, "response_format": response_format,
                                      self._limit_param: max_output_tokens}
            if "temperature" not in self._dropped:
                params["temperature"] = temperature
            if self._reasoning_effort and "reasoning_effort" not in self._dropped:
                params["reasoning_effort"] = self._reasoning_effort
            try:
                completion = await self._client.chat.completions.create(**params)
            except openai.APIStatusError as exc:
                if exc.status_code == 400:
                    if adaptations < 3 and self._adapt_to_rejection(exc, params):
                        adaptations += 1
                        continue
                    raise self._request_error(exc) from exc
                if exc.status_code not in (408, 429) and exc.status_code < 500:
                    raise self._request_error(exc) from exc
                retry_after = parse_retry_after(exc.response.headers)
                if transient >= self._max_retries or (retry_after is not None and retry_after > self._max_wait):
                    raise self._request_error(exc, retryable=True) from exc
                delay = self._wait(transient, retry_after)
                logger.warning("foundry HTTP %d; retry %d/%d in %.1fs", exc.status_code, transient + 1,
                               self._max_retries, delay)
            except openai.APIConnectionError as exc:           # includes timeouts
                if transient >= self._max_retries:
                    raise self._request_error(exc, retryable=True) from exc
                delay = self._wait(transient, None)
                logger.warning("foundry %s; retry %d/%d in %.1fs", type(exc).__name__, transient + 1,
                               self._max_retries, delay)
            else:
                totals.add(completion.usage)
                return completion
            await self._sleep(delay)
            transient += 1

    def _wait(self, attempt: int, retry_after: float | None) -> float:
        return backoff_delay(attempt, base=self._backoff_base, cap=self._backoff_cap, retry_after=retry_after,
                             jitter=self._jitter)

    def _adapt_to_rejection(self, exc: openai.APIStatusError, sent: dict[str, Any]) -> bool:
        """Learns from a 400 that names an unsupported parameter; True when the request should be retried.

        ``sent`` is the request that was rejected. Concurrent calls may all be in flight with the parameter before the
        first rejection teaches the adapter, so a rejection of something we really sent is always worth one retry
        (without it), even when another call has already recorded the lesson.
        """
        rejected = _rejected_param(exc)
        if rejected in ("temperature", "reasoning_effort"):
            if rejected not in sent:
                return False
            if rejected not in self._dropped:
                self._dropped.add(rejected)
                logger.info("foundry deployment %s does not accept %s; continuing without it", self.model, rejected)
            return True
        if self._limit_auto and rejected in ("max_tokens", "max_completion_tokens") and rejected in sent:
            if rejected == self._limit_param:                 # a concurrent call may already have switched
                self._limit_param = "max_tokens" if rejected == "max_completion_tokens" else "max_completion_tokens"
                logger.info("foundry deployment %s rejected %s; switching to %s", self.model, rejected, self._limit_param)
            return True
        return False

    def _repair_messages(
        self, messages: list[dict[str, Any]], content: str | None, problem: str, finish_reason: str | None,
    ) -> list[dict[str, Any]]:
        note = f"Your previous reply could not be used: {problem}."
        if finish_reason == "length":
            note += " It was cut off by the output limit; answer again with a more compact JSON object."
        note += " Reply again with ONLY one valid JSON object that conforms exactly to the required schema."
        repaired = list(messages)
        if content:
            repaired.append({"role": "assistant", "content": content[: self._repair_echo_chars]})
        repaired.append({"role": "user", "content": note})
        return repaired

    def _request_error(self, exc: openai.APIError, *, retryable: bool = False) -> LlmRequestError:
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        text = f"Foundry model call failed ({self.model}): " + (f"HTTP {status} " if status else "") + (
            f"{code}: " if code else "") + _error_text(exc)[:300]
        hint = {
            401: f"authentication failed - with Entra assign 'Cognitive Services OpenAI User' to the identity "
                 f"(token scope {self._scope}); with a key check VOICECRM_FOUNDRY_KEY",
            403: "access denied - check the 'Cognitive Services OpenAI User' role and network rules on the resource",
            404: "deployment or endpoint not found - check VOICECRM_FOUNDRY_ENDPOINT and VOICECRM_FOUNDRY_DEPLOYMENT",
            429: "rate limited after retries - raise the deployment's quota or lower the request rate",
        }.get(status or 0)
        if hint:
            text += f" ({hint})"
        return LlmRequestError(text, status_code=status, code=str(code) if code else None, retryable=retryable)


# ---- helpers --------------------------------------------------------------------------------------------------------

def _v1_base_url(endpoint: str) -> str:
    base = endpoint.strip().rstrip("/")
    if "/openai/v1" not in base:
        base += "/openai/v1"
    return base + "/"


def _schema_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "response"


def _error_text(exc: Any) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return str(getattr(exc, "message", None) or exc)


def _rejected_param(exc: Any) -> str | None:
    param = getattr(exc, "param", None)
    if param:
        return str(param)
    text = _error_text(exc).lower()
    named = re.search(r"(?:unsupported parameter|unrecognized request argument supplied|unknown parameter)\W+(\w+)", text)
    if named:
        return named.group(1)
    if "temperature" in text and any(marker in text for marker in ("only the default", "unsupported", "not support")):
        return "temperature"
    return None


def _parse_json_object(content: str | None, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(object, None) when the reply is a JSON object with the schema's top-level required keys, else (None, problem)."""
    if content is None or not content.strip():
        return None, "empty reply"
    text = content.strip()
    if text.startswith("```"):                               # tolerate a markdown fence around otherwise valid JSON
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON ({exc.msg} at character {exc.pos})"
    if not isinstance(data, dict):
        return None, "the top-level JSON value is not an object"
    required = schema.get("required") if isinstance(schema, dict) else None
    missing = [k for k in (required or []) if k not in data]
    if missing:
        return None, f"missing required properties: {', '.join(missing[:5])}"
    return data, None

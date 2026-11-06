"""Shared plumbing for the live Azure adapters (Speech, Language, Search, Foundry): auth headers, retry, errors.

Private to the adapters that need it. It lives beside the transcription adapters because ``adapters/`` itself is
owned by another slice; the module has no dependency on the rest of the package.

Auth model (``VOICECRM_AZURE_AUTH``): ``entra`` acquires a bearer token with ``azure.identity`` (async
``DefaultAzureCredential`` when ``aiohttp`` is installed, otherwise the synchronous one in a worker thread);
``key`` sends the service's API-key header. Nothing here logs request or response bodies.
"""
from __future__ import annotations

import asyncio
import email.utils
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
Sleep = Callable[[float], Awaitable[None]]


class AzureServiceError(RuntimeError):
    """A live Azure service call failed. The message never contains request content (audio, transcript text)."""

    def __init__(
        self, message: str, *, service: str, status_code: int | None = None, code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class EntraTokenProvider:
    """Caches one Entra access token for a scope and refreshes it shortly before it expires."""

    def __init__(
        self, scope: str, credential: Any | None = None, *, skew_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._scope = scope
        self._credential = credential
        self._owns_credential = credential is None
        self._skew = skew_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_on = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and self._expires_on - self._skew > self._clock():
                return self._token
            if self._credential is None:
                self._credential = self._create_default_credential()
            get_token = self._credential.get_token
            if inspect.iscoroutinefunction(get_token):
                access = await get_token(self._scope)
            else:
                access = await asyncio.to_thread(get_token, self._scope)
            self._token, self._expires_on = access.token, float(access.expires_on)
            return self._token

    @staticmethod
    def _create_default_credential() -> Any:
        try:
            from azure.identity.aio import DefaultAzureCredential

            return DefaultAzureCredential()
        except ImportError:
            # azure-identity's async credentials need aiohttp; the sync credential in a thread is equivalent here.
            logger.warning("aiohttp is not installed; using the synchronous DefaultAzureCredential in a worker thread")
            from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential

            return SyncDefaultAzureCredential()

    async def aclose(self) -> None:
        credential, self._credential = self._credential, None
        if credential is None or not self._owns_credential:
            return
        close = getattr(credential, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


class AzureAuth:
    """Builds the auth headers for one Azure data-plane service from ``entra`` or ``key`` settings."""

    def __init__(
        self, *, service: str, mode: Literal["entra", "key"], key: str | None, scope: str, key_header: str,
        credential: Any | None = None,
    ) -> None:
        if mode not in ("entra", "key"):
            raise ValueError(f"{service}: auth must be 'entra' or 'key', got {mode!r}")
        if mode == "key" and not key:
            raise ValueError(f"{service}: auth='key' requires an API key (VOICECRM_AZURE_AUTH=key)")
        self.service = service
        self.mode = mode
        self._key = key
        self._key_header = key_header
        self._tokens = EntraTokenProvider(scope, credential) if mode == "entra" else None

    async def headers(self) -> dict[str, str]:
        if self._tokens is None:
            return {self._key_header: str(self._key)}
        return {"Authorization": f"Bearer {await self._tokens.get()}"}

    async def token(self) -> str:
        """Bearer token (entra mode) or the raw key (key mode), for SDKs that take a credential callable."""
        if self._tokens is None:
            return str(self._key)
        return await self._tokens.get()

    async def aclose(self) -> None:
        if self._tokens is not None:
            await self._tokens.aclose()


def parse_retry_after(headers: Mapping[str, str], *, now: Callable[[], float] = time.time) -> float | None:
    """Seconds to wait according to ``retry-after-ms`` / ``retry-after`` (delta-seconds or HTTP date), if present."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    for name, scale in (("retry-after-ms", 0.001), ("x-ms-retry-after-ms", 0.001)):
        if name in lowered:
            try:
                return max(0.0, float(lowered[name]) * scale)
            except ValueError:
                pass
    raw = lowered.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - now())


def backoff_delay(
    attempt: int, *, base: float, cap: float, retry_after: float | None = None, jitter: float = 0.1,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff (``base * 2**attempt``, capped), never shorter than a wait the service asked for; adds jitter.

    Retry-After is a floor, not an override: Azure OpenAI answers a request throttled by the TPM quota with a wait of a
    few hundred milliseconds even while the quota stays exhausted, so obeying only that spends every retry in a second.
    """
    delay = min(cap, base * (2 ** attempt))
    if retry_after is not None:
        delay = max(delay, retry_after)
    if jitter > 0 and delay > 0:
        delay += (rng or random).uniform(0, jitter * delay)
    return delay


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    service: str,
    max_retries: int,
    backoff_base: float,
    backoff_cap: float,
    sleep: Sleep = asyncio.sleep,
    jitter: float = 0.1,
    max_wait: float = 120.0,
) -> httpx.Response:
    """Runs ``send`` and retries on 408/429/5xx and transport errors, honouring Retry-After.

    Returns the last response (the caller decides what a non-2xx means). A Retry-After longer than ``max_wait``
    stops retrying so a throttled call fails fast instead of hanging the pipeline.
    """
    attempt = 0
    while True:
        try:
            response = await send()
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise AzureServiceError(
                    f"{service}: request failed after {attempt + 1} attempt(s): {type(exc).__name__}",
                    service=service, retryable=True,
                ) from exc
            delay = backoff_delay(attempt, base=backoff_base, cap=backoff_cap, jitter=jitter)
            logger.warning("%s transport error (%s); retry %d/%d in %.1fs", service, type(exc).__name__,
                           attempt + 1, max_retries, delay)
        else:
            if response.status_code not in RETRYABLE_STATUS or attempt >= max_retries:
                return response
            retry_after = parse_retry_after(response.headers)
            if retry_after is not None and retry_after > max_wait:
                return response
            delay = backoff_delay(attempt, base=backoff_base, cap=backoff_cap, retry_after=retry_after, jitter=jitter)
            logger.warning("%s returned HTTP %d; retry %d/%d in %.1fs", service, response.status_code,
                           attempt + 1, max_retries, delay)
        await sleep(delay)
        attempt += 1


def error_details(response: httpx.Response, *, limit: int = 300) -> tuple[str | None, str]:
    """(code, message) from an Azure error body in any of the common shapes; never raises.

    The most specific code wins (``innerError.code`` such as ``NoLanguageIdentified`` over ``InvalidRequest``).
    """
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:limit].strip() or response.reason_phrase
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return None, str(error)[:limit]
    inner = error.get("innerError") or error.get("innererror") or {}
    inner = inner if isinstance(inner, dict) else {}
    code = inner.get("code") or error.get("code")
    message = error.get("message") or inner.get("message") or response.reason_phrase
    return (str(code) if code else None), str(message)[:limit]


def endpoint_host(endpoint: str) -> str:
    return httpx.URL(endpoint).host.lower()

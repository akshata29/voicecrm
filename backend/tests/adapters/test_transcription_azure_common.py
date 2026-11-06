"""Shared Azure plumbing: Entra token caching, Retry-After parsing, backoff, retry loop and error summaries."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from azure.core.credentials import AccessToken

from voicecrm.adapters.transcription._azure_common import (
    AzureAuth,
    AzureServiceError,
    EntraTokenProvider,
    backoff_delay,
    error_details,
    parse_retry_after,
    send_with_retry,
)


class AsyncCred:
    def __init__(self, expires_in: float = 3600.0) -> None:
        self.calls = 0
        self.expires_in = expires_in
        self.closed = False

    async def get_token(self, *scopes: str, **_kw: object) -> AccessToken:
        self.calls += 1
        return AccessToken(f"token-{self.calls}", int(1_000_000 + self.expires_in))

    async def close(self) -> None:
        self.closed = True


class SyncCred:
    def __init__(self) -> None:
        self.closed = False

    def get_token(self, *scopes: str, **_kw: object) -> AccessToken:
        return AccessToken("sync-token", 2_000_000_000)

    def close(self) -> None:
        self.closed = True


# ---- Retry-After and backoff ------------------------------------------------------------------------------------

def test_parse_retry_after_variants():
    assert parse_retry_after({"Retry-After": "3"}) == 3.0
    assert parse_retry_after({"retry-after": "1.5"}) == 1.5
    assert parse_retry_after({"retry-after-ms": "250"}) == pytest.approx(0.25)
    assert parse_retry_after({"x-ms-retry-after-ms": "1000"}) == 1.0
    assert parse_retry_after({}) is None
    assert parse_retry_after({"retry-after": "soon"}) is None
    when = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    now = (when - timedelta(seconds=30)).timestamp()
    assert parse_retry_after({"Retry-After": format_datetime(when, usegmt=True)}, now=lambda: now) == pytest.approx(30.0)
    assert parse_retry_after({"Retry-After": format_datetime(when, usegmt=True)}, now=lambda: now + 999) == 0.0


def test_backoff_delay_is_exponential_capped_and_respects_retry_after():
    assert [backoff_delay(i, base=1.0, cap=5.0, jitter=0) for i in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert backoff_delay(0, base=1.0, cap=5.0, retry_after=7.0, jitter=0) == 7.0
    jittered = backoff_delay(2, base=1.0, cap=30.0, jitter=0.5, rng=random.Random(1))
    assert 4.0 <= jittered <= 6.0


def test_a_tiny_retry_after_does_not_defeat_the_exponential_backoff():
    # Azure OpenAI answers a TPM-throttled call with a few hundred ms; obeying only that burns all retries at once
    assert [backoff_delay(i, base=1.0, cap=30.0, retry_after=0.2, jitter=0) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff_delay(1, base=1.0, cap=30.0, retry_after=0.0, jitter=0) == 2.0
    assert backoff_delay(1, base=1.0, cap=30.0, retry_after=9.0, jitter=0) == 9.0      # a longer wait is still honoured


# ---- tokens -----------------------------------------------------------------------------------------------------

async def test_token_is_cached_until_close_to_expiry():
    now = [1_000_000.0 - 10_000]
    cred = AsyncCred(expires_in=3600)
    tokens = EntraTokenProvider("scope", cred, skew_seconds=120, clock=lambda: now[0])
    assert await tokens.get() == "token-1"
    assert await tokens.get() == "token-1" and cred.calls == 1
    now[0] = 1_000_000 + 3600 - 60                       # inside the refresh window
    assert await tokens.get() == "token-2" and cred.calls == 2


async def test_sync_credentials_run_in_a_worker_thread():
    assert await EntraTokenProvider("scope", SyncCred(), clock=lambda: 0.0).get() == "sync-token"


async def test_default_credential_falls_back_to_sync_when_aiohttp_is_missing(monkeypatch):
    import azure.identity
    import azure.identity.aio

    def boom() -> None:
        raise ImportError("aiohttp package is not installed")

    sync = SyncCred()
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", boom)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: sync)
    tokens = EntraTokenProvider("scope", clock=lambda: 0.0)
    assert await tokens.get() == "sync-token"
    await tokens.aclose()
    assert sync.closed is True                           # we created it, so we close it


async def test_injected_credentials_are_not_closed():
    cred = AsyncCred()
    tokens = EntraTokenProvider("scope", cred, clock=lambda: 0.0)
    await tokens.get()
    await tokens.aclose()
    assert cred.closed is False


async def test_auth_headers_for_both_modes():
    key = AzureAuth(service="svc", mode="key", key="abc", scope="s", key_header="api-key")
    assert await key.headers() == {"api-key": "abc"} and await key.token() == "abc"
    entra = AzureAuth(service="svc", mode="entra", key=None, scope="s", key_header="api-key", credential=SyncCred())
    assert await entra.headers() == {"Authorization": "Bearer sync-token"}
    with pytest.raises(ValueError, match="requires an API key"):
        AzureAuth(service="svc", mode="key", key="", scope="s", key_header="api-key")
    with pytest.raises(ValueError, match="'entra' or 'key'"):
        AzureAuth(service="svc", mode="oauth", key="x", scope="s", key_header="api-key")  # type: ignore[arg-type]


# ---- retry loop and errors --------------------------------------------------------------------------------------

async def test_send_with_retry_retries_retryable_statuses_only():
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    responses = iter([httpx.Response(502), httpx.Response(429, headers={"retry-after-ms": "500"}), httpx.Response(200)])

    async def send() -> httpx.Response:
        return next(responses)

    response = await send_with_retry(send, service="svc", max_retries=5, backoff_base=0.1, backoff_cap=1.0,
                                     sleep=sleep, jitter=0)
    assert response.status_code == 200 and sleeps == [0.1, 0.5]

    async def not_found() -> httpx.Response:
        return httpx.Response(404)

    assert (await send_with_retry(not_found, service="svc", max_retries=5, backoff_base=0, backoff_cap=0,
                                  sleep=sleep)).status_code == 404 and len(sleeps) == 2


async def test_send_with_retry_raises_a_clean_error_for_persistent_transport_failures():
    async def send() -> httpx.Response:
        raise httpx.ReadTimeout("secret details are not echoed")

    async def sleep(_d: float) -> None:
        return None

    with pytest.raises(AzureServiceError, match="svc: request failed after 3 attempt") as info:
        await send_with_retry(send, service="svc", max_retries=2, backoff_base=0, backoff_cap=0, sleep=sleep)
    assert "secret" not in str(info.value) and info.value.retryable


def test_error_details_shapes():
    def resp(payload: object = None, **kw) -> httpx.Response:
        return httpx.Response(400, json=payload, **kw) if payload is not None else httpx.Response(400, **kw)

    assert error_details(resp({"error": {"code": "InvalidRequest", "message": "bad"}})) == ("InvalidRequest", "bad")
    assert error_details(resp({"code": "X", "message": "flat", "innerError": {"code": "Detailed"}})) == ("Detailed", "flat")
    assert error_details(resp({"error": {"code": "A", "message": "m", "innererror": {"code": "B"}}})) == ("B", "m")
    assert error_details(resp(text="plain text body")) == (None, "plain text body")
    assert error_details(resp(["not", "a", "dict"])) == (None, "Bad Request")
    assert error_details(resp({"error": "string error"}))[1] == "string error"
    assert len(error_details(resp({"error": {"message": "x" * 1000}}))[1]) == 300

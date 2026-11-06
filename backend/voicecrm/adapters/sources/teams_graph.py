"""Microsoft Graph access for live Teams ingestion: an app-only token, retries, and errors that say how to fix them.

Listing transcripts (`onlineMeeting: getAllTranscripts`) is an *application-permission* API: there is no delegated variant.
So the app signs in with the client-credentials flow (tenant id, client id, client secret) and needs, granted with admin
consent: `OnlineMeetingTranscript.Read.All` and `OnlineMeetings.Read.All` (meeting subject and attendees), plus
`User.Read.All` only if organisers are configured by UPN rather than object id. Three tenant-side switches also gate it:

  1. Teams admin centre > Meetings > Meeting settings > Transcript API access > Microsoft Graph access = On (default Off);
     `GraphAccessToTranscriptsDisabled` otherwise.
  2. "Include speaker attribution" = On (default Off) for speaker names; `SpeakerAttributionNotAllowed` otherwise.
  3. An application access policy granting the app access to each organiser (`New-CsApplicationAccessPolicy` +
     `Grant-CsApplicationAccessPolicy`); it takes up to 30 minutes to apply.

Only scheduled meetings that have a calendar event are supported (not "Meet now"). Secrets never appear in messages.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .teams import GRAPH_BASE, GRAPH_SCOPE

logger = logging.getLogger(__name__)

LOGIN_AUTHORITY = "https://login.microsoftonline.com"
RETRY_STATUS = frozenset({429, 502, 503, 504})
TOKEN_SKEW_SECONDS = 120

_AAD_HINTS = {
    "AADSTS7000215": "the client secret is wrong: use the secret VALUE (shown once when it was created), not its id",
    "AADSTS7000222": "the client secret has expired: create a new one",
    "AADSTS700016": "no application with that client id exists in this tenant: check VOICECRM_TEAMS_CLIENT_ID and "
                    "VOICECRM_TEAMS_TENANT_ID",
    "AADSTS90002": "the tenant was not found: check VOICECRM_TEAMS_TENANT_ID",
    "AADSTS900023": "the tenant id is not valid: check VOICECRM_TEAMS_TENANT_ID",
    "AADSTS7000112": "the application is disabled in the tenant: enable it under Enterprise applications",
}
_POLICY_HINT = (
    "grant the app an application access policy for this organiser (Teams PowerShell: New-CsApplicationAccessPolicy "
    "-Identity VoiceCRM -AppIds <client id>; Grant-CsApplicationAccessPolicy -PolicyName VoiceCRM -Identity <user object id>"
    " - or -Global), then wait up to 30 minutes"
)
_PERMISSION_HINT = (
    "the app is not allowed to read this: add the APPLICATION permissions OnlineMeetingTranscript.Read.All and "
    "OnlineMeetings.Read.All, grant admin consent, and create the application access policy (see docs/live-demo.md)"
)


class GraphError(RuntimeError):
    """A failed Microsoft Graph or sign-in call, with the status, the service's error code and what to do about it."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, hint: str | None = None) -> None:
        super().__init__(f"{message} - {hint}" if hint else message)
        self.message, self.status, self.code, self.hint = message, status, code, hint


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        error = response.json().get("error", {})
        inner = error.get("innerError") or {}
        return (inner.get("code") or error.get("code")), error.get("message")
    except (ValueError, AttributeError):
        return None, None


def graph_error(response: httpx.Response) -> GraphError:
    code, message = _error_fields(response)
    status = response.status_code
    lowered = (message or "").lower()
    hint: str | None = None
    if code == "GraphAccessToTranscriptsDisabled":
        hint = ("a Teams admin must turn on Microsoft Graph access: Teams admin centre > Meetings > Meeting settings > "
                "Transcript API access (PowerShell: Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true)")
    elif code == "SpeakerAttributionNotAllowed":
        hint = ("turn on 'Include speaker attribution' under Transcript API access (PowerShell: Set-CsTeamsMeetingConfiguration "
                "-EnableAttributedTranscripts $true); without it the transcript has no speaker names")
    elif status == 403 and "application access policy" in lowered:
        hint = _POLICY_HINT
    elif status == 403:
        hint = _PERMISSION_HINT
    elif status == 401:
        hint = "the token was rejected: check the app registration, admin consent and VOICECRM_TEAMS_TENANT_ID"
    elif status == 404:
        hint = ("not found: the meeting or transcript does not exist for this organiser, has expired, or was not scheduled "
                "from a calendar event ('Meet now' meetings are not supported)")
    detail = message or response.reason_phrase or "request failed"
    return GraphError(f"Graph {status}{f' {code}' if code else ''}: {detail[:300]}", status=status, code=code, hint=hint)


class ClientSecretAuth:
    """OAuth2 client-credentials token for Microsoft Graph, cached until shortly before it expires."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, scope: str = GRAPH_SCOPE,
                 authority: str = LOGIN_AUTHORITY, http_client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        missing = [name for name, value in (("tenant id", tenant_id), ("client id", client_id),
                                            ("client secret", client_secret)) if not value]
        if missing:
            raise ValueError(f"Teams live ingestion needs the app's {', '.join(missing)} "
                             "(VOICECRM_TEAMS_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET)")
        self._tenant, self._client_id, self._secret, self._scope = tenant_id, client_id, client_secret, scope
        self._authority = authority.rstrip("/")
        self._client = http_client
        self._owns_client = http_client is None
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and self._clock() < self._expires_at - TOKEN_SKEW_SECONDS:
                return self._token
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
            response = await self._client.post(
                f"{self._authority}/{self._tenant}/oauth2/v2.0/token",
                data={"client_id": self._client_id, "client_secret": self._secret, "scope": self._scope,
                      "grant_type": "client_credentials"})
            if response.status_code != 200:
                raise self._error(response)
            body = response.json()
            self._token = str(body["access_token"])
            self._expires_at = self._clock() + float(body.get("expires_in", 3600))
            return self._token

    def _error(self, response: httpx.Response) -> GraphError:
        try:
            body = response.json()
            description = str(body.get("error_description") or body.get("error") or "")
        except ValueError:
            description = response.reason_phrase or ""
        match = re.search(r"AADSTS\d+", description)
        code = match.group(0) if match else None
        first_line = description.splitlines()[0] if description else "sign-in failed"
        return GraphError(f"Microsoft Entra sign-in failed (HTTP {response.status_code}): {first_line[:260]}",
                          status=response.status_code, code=code, hint=_AAD_HINTS.get(code or ""))

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class GraphClient:
    """Authenticated Graph requests with 401 refresh, throttling-aware retries and paging."""

    def __init__(self, auth: ClientSecretAuth, *, base_url: str = GRAPH_BASE, http_client: httpx.AsyncClient | None = None,
                 max_retries: int = 4, backoff_base: float = 1.0, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._auth = auth
        self._base = base_url.rstrip("/")
        self._client = http_client
        self._owns_client = http_client is None
        self._max_retries = max_retries
        self._backoff = backoff_base
        self._sleep = sleep

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        return self._client

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self._base}{path if path.startswith('/') else '/' + path}"

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None,
                      accept: str = "application/json") -> httpx.Response:
        url, refreshed, force = self._url(path), False, False
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {await self._auth.token(force=force)}", "Accept": accept}
            force = False
            response = await self._http().request(method, url, params=params, json=json, headers=headers)
            if response.status_code == 401 and not refreshed:
                refreshed = force = True
                continue
            if response.status_code in RETRY_STATUS and attempt < self._max_retries:
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = self._backoff * 2 ** attempt
                logger.warning("Graph %s on %s; retrying in %.1fs", response.status_code, method, delay)
                await self._sleep(min(delay, 60.0))
                continue
            return response
        raise RuntimeError("unreachable")

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.request("GET", path, params=params)
        if response.status_code != 200:
            raise graph_error(response)
        return dict(response.json())

    async def get_text(self, path: str, *, accept: str) -> str:
        response = await self.request("GET", path, accept=accept)
        if response.status_code != 200:
            raise graph_error(response)
        return response.content.decode("utf-8-sig")

    async def paged(self, path: str, *, params: dict[str, Any] | None = None, limit: int = 1000) -> AsyncIterator[dict[str, Any]]:
        """Items of a collection, following @odata.nextLink (the first request carries ``params``)."""
        target: str | None = path
        first, seen = True, 0
        while target and seen < limit:
            body = await self.get_json(target, params=params if first else None)
            first = False
            for item in body.get("value", []):
                seen += 1
                yield item
            target = body.get("@odata.nextLink")

    # ---- change-notification subscriptions ---------------------------------------------------------------------
    async def create_subscription(self, *, resource: str, notification_url: str, client_state: str, minutes: int,
                                  lifecycle_url: str | None = None) -> dict[str, Any]:
        expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        body: dict[str, Any] = {"changeType": "created", "notificationUrl": notification_url, "resource": resource,
                                "expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                                "clientState": client_state}
        if lifecycle_url:
            body["lifecycleNotificationUrl"] = lifecycle_url
        response = await self.request("POST", "/subscriptions", json=body)
        if response.status_code not in (200, 201):
            raise graph_error(response)
        return dict(response.json())

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        return [item async for item in self.paged("/subscriptions")]

    async def renew_subscription(self, subscription_id: str, minutes: int) -> dict[str, Any]:
        expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        response = await self.request("PATCH", f"/subscriptions/{subscription_id}",
                                      json={"expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")})
        if response.status_code != 200:
            raise graph_error(response)
        return dict(response.json())

    async def delete_subscription(self, subscription_id: str) -> None:
        response = await self.request("DELETE", f"/subscriptions/{subscription_id}")
        if response.status_code not in (200, 204):
            raise graph_error(response)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

"""Event Grid subscriptions on the Communication Services resource, managed through Azure Resource Manager.

Communication Services announces ``IncomingCall`` (a call is ringing the line and must be answered within 30 seconds) and
``RecordingFileStatusUpdated`` (a recording is ready to download) through Event Grid. Two subscriptions send them to the same
webhook so each event type gets the delivery policy it needs: a ringing call is useless after a minute, while a recording
notice must survive an app that is down for a while.

The app's own Entra credential signs the requests; the identity needs Contributor (or EventGrid EventSubscription
Contributor) on the Communication Services resource. Creating a webhook subscription makes Event Grid call the URL once and
expect the validation code back, so the app (with its phone ingress) and the tunnel must be running.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

ARM = "https://management.azure.com"
API_VERSION = "2024-06-01-preview"
SCOPE = f"{ARM}/.default"
PREFIX = "voicecrm-phone-"
_RESOURCE = re.compile(r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft\.Communication/"
                       r"communicationServices/[^/]+$", re.IGNORECASE)
_KEY = re.compile(r"([?&]key=)[^&#]+")

# (subscription name, event type, delivery attempts, time to live in minutes)
PLANS = (
    (f"{PREFIX}calls", "Microsoft.Communication.IncomingCall", 2, 1),
    (f"{PREFIX}recordings", "Microsoft.Communication.RecordingFileStatusUpdated", 30, 1440),
)
_FAILED = {"Failed", "Canceled", "AwaitingManualAction"}


class ArmError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, hint: str = "") -> None:
        super().__init__(message)
        self.status, self.hint = status, hint


@dataclass(frozen=True)
class EventSubscription:
    name: str
    state: str
    event_types: tuple[str, ...]
    endpoint: str                                   # the webhook URL with its key masked


def mask_key(url: str) -> str:
    return _KEY.sub(r"\1***", url)


def _hint(status: int, code: str, message: str) -> str:
    low = f"{code} {message}".lower()
    if "missingsubscriptionregistration" in low or "not registered" in low:
        return "register the provider once: az provider register --namespace Microsoft.EventGrid"
    if "validation" in low or "handshake" in low or ("endpoint" in low and status == 400):
        return ("Event Grid could not complete the webhook handshake: the app (with its phone ingress) and the tunnel must be "
                "running, and the key in the URL must match VOICECRM_PHONE_WEBHOOK_SECRET")
    if status in (401, 403):
        return "the signed-in identity needs Contributor on the Communication Services resource (az login to the right tenant)"
    if status == 404:
        return "not found: check VOICECRM_PHONE_ACS_RESOURCE_ID"
    return ""


class EventGridSubscriptions:
    def __init__(self, resource_id: str, credential: Any, *, http: httpx.AsyncClient | None = None,
                 poll_seconds: float = 2.0, wait_seconds: float = 90.0) -> None:
        if not _RESOURCE.match(resource_id.strip()):
            raise ValueError("VOICECRM_PHONE_ACS_RESOURCE_ID must look like /subscriptions/<id>/resourceGroups/<group>/providers/"
                             "Microsoft.Communication/communicationServices/<name>")
        self._scope = resource_id.strip()
        self._credential, self._http = credential, http
        self._owns_http = http is None
        self._poll, self._wait = poll_seconds, wait_seconds

    # ---- requests ------------------------------------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        return self._http

    def _url(self, name: str | None = None) -> str:
        base = f"{ARM}{self._scope}/providers/Microsoft.EventGrid/eventSubscriptions"
        return f"{base}/{name}" if name else base

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._credential.get_token(SCOPE)
        response = await self._client().request(method, url, params={"api-version": API_VERSION},
                                                headers={"Authorization": f"Bearer {token.token}"}, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json().get("error") or {}
            except ValueError:
                error = {}
            code, message = str(error.get("code") or ""), " ".join(str(error.get("message") or response.reason_phrase).split())
            raise ArmError(f"Azure Resource Manager refused the request: HTTP {response.status_code} {code} {message[:300]}".strip(),
                           status=response.status_code, hint=_hint(response.status_code, code, message))
        return response

    @staticmethod
    def _parse(item: dict[str, Any]) -> EventSubscription:
        props = item.get("properties") or {}
        destination = (props.get("destination") or {}).get("properties") or {}
        return EventSubscription(
            name=str(item.get("name") or ""), state=str(props.get("provisioningState") or "Unknown"),
            event_types=tuple((props.get("filter") or {}).get("includedEventTypes") or ()),
            endpoint=mask_key(str(destination.get("endpointUrl") or "")))

    # ---- operations ------------------------------------------------------------------------------------------------
    async def list(self) -> list[EventSubscription]:
        response = await self._request("GET", self._url())
        found = [self._parse(i) for i in response.json().get("value") or []]
        return [s for s in found if s.name.startswith(PREFIX)]

    async def ensure(self, endpoint_url: str) -> list[EventSubscription]:
        """Creates (or updates) both subscriptions and waits until Event Grid has validated the webhook."""
        if not endpoint_url.lower().startswith("https://"):
            raise ArmError("the webhook URL must be https", hint="use the dev tunnel's https address in VOICECRM_PHONE_PUBLIC_BASE_URL")
        created = []
        for name, event_type, attempts, ttl in PLANS:
            body = {"properties": {
                "destination": {"endpointType": "WebHook", "properties": {"endpointUrl": endpoint_url, "maxEventsPerBatch": 1,
                                                                          "preferredBatchSizeInKilobytes": 64}},
                "filter": {"includedEventTypes": [event_type]},
                "eventDeliverySchema": "EventGridSchema",
                "retryPolicy": {"maxDeliveryAttempts": attempts, "eventTimeToLiveInMinutes": ttl}}}
            await self._request("PUT", self._url(name), json=body)
            created.append(await self._until_ready(name))
        return created

    async def _until_ready(self, name: str) -> EventSubscription:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait
        while True:
            current = self._parse((await self._request("GET", self._url(name))).json())
            if current.state == "Succeeded":
                return current
            if current.state in _FAILED:
                hint = (_hint(400, "validation", "") if current.state == "AwaitingManualAction"
                        else "check the app and the tunnel are running, then run the command again")
                raise ArmError(f"the subscription {name} ended in state {current.state}", hint=hint)
            if loop.time() > deadline:
                raise ArmError(f"the subscription {name} was still {current.state} after {self._wait:.0f} s",
                               hint="Event Grid is waiting for the webhook handshake: is the app (with its phone ingress) reachable?")
            await asyncio.sleep(self._poll)

    async def delete(self) -> list[str]:
        removed = []
        for name, *_ in PLANS:
            try:
                await self._request("DELETE", self._url(name))
            except ArmError as exc:
                if exc.status != 404:
                    raise
                continue
            removed.append(name)
        return removed

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

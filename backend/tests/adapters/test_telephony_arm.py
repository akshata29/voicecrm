"""Event Grid subscriptions on the Communication Services resource, through Azure Resource Manager (HTTP mocked)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from voicecrm.adapters.telephony.arm import API_VERSION, ArmError, EventGridSubscriptions, mask_key

RESOURCE = ("/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-demo/providers/"
            "Microsoft.Communication/communicationServices/acs-demo")
BASE = f"https://management.azure.com{RESOURCE}/providers/Microsoft.EventGrid/eventSubscriptions"
WEBHOOK = "https://line.example.test/api/v1/webhooks/phone/events?key=super-secret-key"
CALLS, RECORDINGS = f"{BASE}/voicecrm-phone-calls", f"{BASE}/voicecrm-phone-recordings"


class Credential:
    def __init__(self) -> None:
        self.scopes: list[tuple] = []

    async def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return SimpleNamespace(token="arm-token")


def subscription(name: str, state: str = "Succeeded", event_type: str = "Microsoft.Communication.IncomingCall") -> dict:
    return {"name": name, "properties": {"provisioningState": state, "filter": {"includedEventTypes": [event_type]},
                                         "destination": {"endpointType": "WebHook", "properties": {"endpointUrl": WEBHOOK}}}}


def arm_error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


@pytest.fixture()
def arm():
    with respx.mock(assert_all_called=False) as router:
        yield router


def manager(**kw) -> tuple[EventGridSubscriptions, Credential]:
    credential = Credential()
    return EventGridSubscriptions(RESOURCE, credential, poll_seconds=0, wait_seconds=kw.pop("wait_seconds", 5), **kw), credential


def test_the_resource_id_must_be_the_arm_id_of_a_communication_services_resource():
    for bad in ("", "acs-demo", "https://acs.communication.azure.com",
                "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/x"):
        with pytest.raises(ValueError, match="VOICECRM_PHONE_ACS_RESOURCE_ID"):
            EventGridSubscriptions(bad, Credential())


def test_the_key_in_a_url_is_masked():
    assert mask_key(WEBHOOK) == "https://line.example.test/api/v1/webhooks/phone/events?key=***"
    assert mask_key("https://x.example/a?b=1&key=abc&c=2") == "https://x.example/a?b=1&key=***&c=2"
    assert mask_key("https://x.example/a") == "https://x.example/a"


async def test_both_subscriptions_are_created_with_their_own_delivery_policy(arm):
    puts = {
        "calls": arm.put(CALLS).mock(return_value=httpx.Response(201, json=subscription("voicecrm-phone-calls", "Creating"))),
        "recordings": arm.put(RECORDINGS).mock(return_value=httpx.Response(201, json=subscription("voicecrm-phone-recordings", "Creating")))}
    arm.get(CALLS).mock(return_value=httpx.Response(200, json=subscription("voicecrm-phone-calls")))
    arm.get(RECORDINGS).mock(return_value=httpx.Response(200, json=subscription(
        "voicecrm-phone-recordings", event_type="Microsoft.Communication.RecordingFileStatusUpdated")))
    events, credential = manager()
    created = await events.ensure(WEBHOOK)

    assert [(s.name, s.state) for s in created] == [("voicecrm-phone-calls", "Succeeded"), ("voicecrm-phone-recordings", "Succeeded")]
    assert all("super-secret-key" not in s.endpoint and s.endpoint.endswith("key=***") for s in created)
    calls = json.loads(puts["calls"].calls.last.request.content)["properties"]
    recordings = json.loads(puts["recordings"].calls.last.request.content)["properties"]
    assert calls["filter"] == {"includedEventTypes": ["Microsoft.Communication.IncomingCall"]}
    assert calls["retryPolicy"] == {"maxDeliveryAttempts": 2, "eventTimeToLiveInMinutes": 1}              # a ringing call expires fast
    assert recordings["filter"] == {"includedEventTypes": ["Microsoft.Communication.RecordingFileStatusUpdated"]}
    assert recordings["retryPolicy"] == {"maxDeliveryAttempts": 30, "eventTimeToLiveInMinutes": 1440}      # a recording notice must not be lost
    for body in (calls, recordings):
        assert body["destination"]["endpointType"] == "WebHook" and body["destination"]["properties"]["endpointUrl"] == WEBHOOK
        assert body["eventDeliverySchema"] == "EventGridSchema" and "advancedFilters" not in body["filter"]
    request = puts["calls"].calls.last.request
    assert request.url.params["api-version"] == API_VERSION and request.headers["authorization"] == "Bearer arm-token"
    assert credential.scopes and all(s == ("https://management.azure.com/.default",) for s in credential.scopes)


async def test_it_waits_while_event_grid_validates_the_webhook(arm):
    arm.put(url__regex=rf"{BASE}/.*").mock(return_value=httpx.Response(201, json=subscription("x", "Creating")))
    polled = arm.get(CALLS).mock(side_effect=[httpx.Response(200, json=subscription("voicecrm-phone-calls", "Creating")),
                                              httpx.Response(200, json=subscription("voicecrm-phone-calls", "Creating")),
                                              httpx.Response(200, json=subscription("voicecrm-phone-calls"))])
    arm.get(RECORDINGS).mock(return_value=httpx.Response(200, json=subscription("voicecrm-phone-recordings")))
    created = await manager()[0].ensure(WEBHOOK)
    assert [s.state for s in created] == ["Succeeded", "Succeeded"] and polled.call_count == 3


async def test_a_subscription_that_needs_a_manual_handshake_is_reported_with_the_cause(arm):
    arm.put(url__regex=rf"{BASE}/.*").mock(return_value=httpx.Response(201, json=subscription("x", "AwaitingManualAction")))
    arm.get(CALLS).mock(return_value=httpx.Response(200, json=subscription("voicecrm-phone-calls", "AwaitingManualAction")))
    with pytest.raises(ArmError) as info:
        await manager()[0].ensure(WEBHOOK)
    assert "AwaitingManualAction" in str(info.value) and "tunnel" in info.value.hint


async def test_a_subscription_that_never_finishes_times_out(arm):
    arm.put(url__regex=rf"{BASE}/.*").mock(return_value=httpx.Response(201, json=subscription("x", "Creating")))
    arm.get(CALLS).mock(return_value=httpx.Response(200, json=subscription("voicecrm-phone-calls", "Creating")))
    with pytest.raises(ArmError, match="still Creating"):
        await manager(wait_seconds=0.05)[0].ensure(WEBHOOK)


async def test_the_webhook_must_be_https(arm):
    with pytest.raises(ArmError, match="https") as info:
        await manager()[0].ensure("http://localhost:8011/api/v1/webhooks/phone/events?key=k")
    assert "tunnel" in info.value.hint and not arm.calls


@pytest.mark.parametrize("status,code,message,fragment", [
    (403, "AuthorizationFailed", "the client does not have authorization", "Contributor"),
    (404, "ResourceNotFound", "the resource was not found", "VOICECRM_PHONE_ACS_RESOURCE_ID"),
    (400, "InvalidRequest", "Webhook validation handshake failed for https://line.example.test", "tunnel"),
    (409, "MissingSubscriptionRegistration", "The subscription is not registered to use namespace 'Microsoft.EventGrid'", "az provider register"),
])
async def test_arm_errors_carry_what_to_do(arm, status, code, message, fragment):
    arm.put(CALLS).mock(return_value=arm_error(status, code, message))
    with pytest.raises(ArmError) as info:
        await manager()[0].ensure(WEBHOOK)
    assert info.value.status == status and code in str(info.value) and fragment in info.value.hint


async def test_an_error_without_a_json_body_is_still_reported(arm):
    arm.put(CALLS).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(ArmError, match="HTTP 502"):
        await manager()[0].ensure(WEBHOOK)


async def test_listing_shows_only_this_apps_subscriptions_with_the_key_masked(arm):
    arm.get(BASE).mock(return_value=httpx.Response(200, json={"value": [
        subscription("voicecrm-phone-calls"), subscription("someone-elses-subscription"), subscription("voicecrm-phone-recordings", "Creating")]}))
    listed = await manager()[0].list()
    assert [(s.name, s.state) for s in listed] == [("voicecrm-phone-calls", "Succeeded"), ("voicecrm-phone-recordings", "Creating")]
    assert listed[0].event_types == ("Microsoft.Communication.IncomingCall",) and "super-secret-key" not in listed[0].endpoint


async def test_deleting_removes_both_and_tolerates_ones_that_are_gone(arm):
    arm.delete(CALLS).mock(return_value=httpx.Response(200))
    arm.delete(RECORDINGS).mock(return_value=arm_error(404, "ResourceNotFound", "gone"))
    assert await manager()[0].delete() == ["voicecrm-phone-calls"]
    arm.delete(RECORDINGS).mock(return_value=arm_error(403, "AuthorizationFailed", "no"))
    with pytest.raises(ArmError) as info:
        await manager()[0].delete()
    assert info.value.status == 403


async def test_a_client_it_created_is_closed_and_one_it_was_given_is_not(arm):
    given = httpx.AsyncClient()
    shared = EventGridSubscriptions(RESOURCE, Credential(), http=given)
    await shared.aclose()
    assert not given.is_closed
    await given.aclose()
    own, _ = manager()
    arm.get(BASE).mock(return_value=httpx.Response(200, json={"value": []}))
    await own.list()
    await own.aclose()
    assert own._http is None

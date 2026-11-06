"""Graph access for live Teams ingestion: app-only sign-in, retries, paging, subscriptions and actionable errors (no network)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from voicecrm.adapters.sources.teams_graph import ClientSecretAuth, GraphClient, GraphError, graph_error

TENANT, CLIENT, SECRET = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "s3cr3t-value-123456"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def token_response(token: str = "tok-1", expires: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "expires_in": expires, "token_type": "Bearer"})


def make(clock: Clock | None = None, sleeps: Sleeps | None = None) -> tuple[GraphClient, ClientSecretAuth]:
    auth = ClientSecretAuth(tenant_id=TENANT, client_id=CLIENT, client_secret=SECRET, clock=clock or Clock(),
                            http_client=httpx.AsyncClient(verify=False))
    return GraphClient(auth, http_client=httpx.AsyncClient(verify=False), sleep=sleeps or Sleeps()), auth


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_the_token_request_is_a_client_credentials_grant_for_graph(api):
    route = api.post(TOKEN_URL).mock(return_value=token_response())
    _, auth = make()
    assert await auth.token() == "tok-1"
    form = parse_qs(route.calls.last.request.content.decode())
    assert form == {"client_id": [CLIENT], "client_secret": [SECRET], "scope": ["https://graph.microsoft.com/.default"],
                    "grant_type": ["client_credentials"]}


async def test_the_token_is_cached_until_shortly_before_it_expires(api):
    route = api.post(TOKEN_URL).mock(side_effect=[token_response("one", 3600), token_response("two", 3600),
                                                  token_response("three", 3600)])
    clock = Clock()
    _, auth = make(clock)
    assert await auth.token() == "one" and await auth.token() == "one" and route.call_count == 1
    clock.now += 3600 - 60                                          # inside the 2 minute safety margin
    assert await auth.token() == "two" and route.call_count == 2
    assert await auth.token(force=True) == "three" and route.call_count == 3


@pytest.mark.parametrize("missing", ["tenant_id", "client_id", "client_secret"])
def test_missing_app_settings_are_named(missing):
    kwargs = {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET, missing: ""}
    with pytest.raises(ValueError, match=missing.replace("_", " ")):
        ClientSecretAuth(**kwargs)


@pytest.mark.parametrize("aad,fragment", [
    ("AADSTS7000215: Invalid client secret provided.", "secret VALUE"),
    ("AADSTS7000222: The provided client secret keys are expired.", "expired"),
    ("AADSTS700016: Application with identifier 'x' was not found in the directory.", "VOICECRM_TEAMS_CLIENT_ID"),
    ("AADSTS90002: Tenant 'x' not found.", "VOICECRM_TEAMS_TENANT_ID"),
])
async def test_sign_in_failures_say_what_to_fix_and_never_echo_the_secret(api, aad, fragment):
    api.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client", "error_description": aad + "\r\nTrace ID: 1"}))
    _, auth = make()
    with pytest.raises(GraphError) as info:
        await auth.token()
    assert fragment in str(info.value) and info.value.code == aad.split(":")[0]
    assert SECRET not in str(info.value) and "Trace ID" not in str(info.value)


async def test_requests_carry_the_bearer_token_and_json_is_returned(api):
    api.post(TOKEN_URL).mock(return_value=token_response("abc"))
    route = api.get(f"{GRAPH}/users/u1").mock(return_value=httpx.Response(200, json={"id": "u1"}))
    graph, _ = make()
    assert await graph.get_json("/users/u1") == {"id": "u1"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer abc"
    assert route.calls.last.request.headers["Accept"] == "application/json"


async def test_a_rejected_token_is_refreshed_once(api):
    tokens = api.post(TOKEN_URL).mock(side_effect=[token_response("stale"), token_response("fresh")])
    route = api.get(f"{GRAPH}/users/u1").mock(side_effect=[httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}),
                                                           httpx.Response(200, json={"id": "u1"})])
    graph, _ = make()
    assert await graph.get_json("/users/u1") == {"id": "u1"}
    assert tokens.call_count == 2 and [c.request.headers["Authorization"] for c in route.calls] == ["Bearer stale", "Bearer fresh"]


async def test_throttling_is_retried_after_the_advertised_delay(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    route = api.get(f"{GRAPH}/users/u1").mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}),
                                                           httpx.Response(503), httpx.Response(200, json={"id": "u1"})])
    sleeps = Sleeps()
    graph, _ = make(sleeps=sleeps)
    assert (await graph.get_json("/users/u1"))["id"] == "u1"
    assert route.call_count == 3 and sleeps.delays == [7.0, 2.0]    # Retry-After first, then exponential back-off


async def test_collections_are_followed_across_pages(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    api.get(f"{GRAPH}/things", params={"$top": "2"}).mock(return_value=httpx.Response(200, json={
        "value": [{"n": 1}, {"n": 2}], "@odata.nextLink": f"{GRAPH}/things?$skiptoken=abc"}))
    api.get(f"{GRAPH}/things", params={"$skiptoken": "abc"}).mock(return_value=httpx.Response(200, json={"value": [{"n": 3}]}))
    graph, _ = make()
    assert [i["n"] async for i in graph.paged("/things", params={"$top": 2})] == [1, 2, 3]


async def test_text_content_is_decoded_without_a_byte_order_mark(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    route = api.get(f"{GRAPH}/t/content").mock(return_value=httpx.Response(200, content="\ufeffWEBVTT\n".encode()))
    graph, _ = make()
    assert await graph.get_text("/t/content", accept="text/vtt") == "WEBVTT\n"
    assert route.calls.last.request.headers["Accept"] == "text/vtt"


def error(status: int, code: str | None, message: str = "m") -> httpx.Response:
    inner = {"innerError": {"code": code}} if code else {}
    return httpx.Response(status, json={"error": {"code": "Forbidden", "message": message, **inner}})


@pytest.mark.parametrize("response,fragment", [
    (error(403, "GraphAccessToTranscriptsDisabled"), "Microsoft Graph access"),
    (error(403, "SpeakerAttributionNotAllowed"), "speaker attribution"),
    (error(403, None, "No application access policy found for this app."), "Grant-CsApplicationAccessPolicy"),
    (error(403, None, "Insufficient privileges"), "OnlineMeetingTranscript.Read.All"),
    (error(401, None), "admin consent"),
    (error(404, None), "calendar event"),
])
def test_graph_errors_branch_on_the_inner_code_and_carry_the_fix(response, fragment):
    exc = graph_error(response)
    assert fragment in str(exc) and exc.status == response.status_code


def test_a_non_json_error_body_still_produces_an_error():
    exc = graph_error(httpx.Response(502, text="<html>bad gateway</html>"))
    assert exc.status == 502 and "502" in str(exc)


async def test_get_json_raises_the_mapped_error(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    api.get(f"{GRAPH}/x").mock(return_value=error(403, "GraphAccessToTranscriptsDisabled"))
    graph, _ = make()
    with pytest.raises(GraphError) as info:
        await graph.get_json("/x")
    assert info.value.code == "GraphAccessToTranscriptsDisabled"


async def test_subscriptions_are_created_listed_renewed_and_deleted(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    created = api.post(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(201, json={"id": "sub1"}))
    api.get(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(200, json={"value": [{"id": "sub1"}]}))
    renewed = api.patch(f"{GRAPH}/subscriptions/sub1").mock(return_value=httpx.Response(200, json={"id": "sub1"}))
    deleted = api.delete(f"{GRAPH}/subscriptions/sub1").mock(return_value=httpx.Response(204))
    graph, _ = make()
    sub = await graph.create_subscription(resource="communications/onlineMeetings/getAllTranscripts",
                                          notification_url="https://x.example/api/v1/webhooks/teams",
                                          client_state="state-1", minutes=55)
    assert sub["id"] == "sub1"
    body = json.loads(created.calls.last.request.content)
    assert body["changeType"] == "created" and body["resource"] == "communications/onlineMeetings/getAllTranscripts"
    assert body["clientState"] == "state-1" and "lifecycleNotificationUrl" not in body and "includeResourceData" not in body
    assert body["expirationDateTime"].endswith(".0000000Z")
    await graph.create_subscription(resource="r", notification_url="https://x.example/n", client_state="s", minutes=240,
                                    lifecycle_url="https://x.example/l")
    assert json.loads(created.calls.last.request.content)["lifecycleNotificationUrl"] == "https://x.example/l"
    assert [s["id"] for s in await graph.list_subscriptions()] == ["sub1"]
    await graph.renew_subscription("sub1", 55)
    assert "expirationDateTime" in json.loads(renewed.calls.last.request.content)
    await graph.delete_subscription("sub1")
    assert deleted.call_count == 1


async def test_a_refused_subscription_explains_the_admin_switch(api):
    api.post(TOKEN_URL).mock(return_value=token_response())
    api.post(f"{GRAPH}/subscriptions").mock(return_value=error(403, "GraphAccessToTranscriptsDisabled"))
    graph, _ = make()
    with pytest.raises(GraphError, match="Transcript API access"):
        await graph.create_subscription(resource="r", notification_url="https://x.example/n", client_state="s", minutes=55)

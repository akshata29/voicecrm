"""Phone routes: status, browser tokens, seller registration and the public webhooks (plus the ingress that serves only those)."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voicecrm.adapters.sources import phone as wire
from voicecrm.adapters.telephony.fake import FakeTelephony
from voicecrm.api.phone_ingress import IngressServer, _EmbeddedServer, create_ingress_app
from voicecrm.pipeline.phone_live import PhoneLine
from voicecrm.ports.telephony import TelephonyError

from ..phone_support import (
    CALLER, CONSOLE, LINE, SECRET, Clock, callback_event, incoming_event, phone_settings, recording_event)
from .helpers import API

EVENTS = f"{API}/webhooks/phone/events"
HEALTH = f"{API}/webhooks/phone/health"
EVENT_GRID_HANDSHAKE = [{"id": "1", "eventType": wire.EVENT_VALIDATION, "data": {"validationCode": "abc-123", "validationUrl": "https://x"}}]


@pytest.fixture()
def phone(api_container, monkeypatch, tmp_path):
    """A configured line (fake telephony, a clock that never fires) installed in the shared test container."""
    tel = FakeTelephony()
    line = PhoneLine(api_container.service, tel, settings=phone_settings(tmp_path), sleep=Clock().sleep)
    monkeypatch.setattr(api_container, "phone", line)
    return SimpleNamespace(line=line, tel=tel)


@pytest.fixture()
def broken_line(api_container, monkeypatch, tmp_path):
    line = PhoneLine(None, None, settings=phone_settings(tmp_path), problem="VOICECRM_PHONE_ACS_ENDPOINT is not set")
    monkeypatch.setattr(api_container, "phone", line)
    return line


@pytest.fixture()
def ingress(api_container) -> Iterator[TestClient]:
    """The webhook-only app, exactly what the tunnel exposes."""
    with TestClient(create_ingress_app(api_container), raise_server_exceptions=False) as test_client:
        yield test_client


def wait_for(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("timed out waiting for the background work")


# ---- status ----------------------------------------------------------------------------------------------------------
def test_status_when_the_line_is_off_says_how_to_turn_it_on(client: TestClient):
    body = client.get(f"{API}/sources/phone").json()
    assert body["mode"] == "off" and body["configured"] is False and "VOICECRM_PHONE_MODE=acs" in body["problem"]


def test_status_describes_the_line(client: TestClient, phone):
    body = client.get(f"{API}/sources/phone").json()
    assert (body["mode"], body["configured"], body["number"], body["line_identity"]) == ("acs", True, "+1 800 555 0100", LINE)
    assert body["seller"] == {"mode": "none", "name": None, "online": False} and body["notice_enabled"] is True
    assert (body["active"], body["recent"], body["calls"], body["last_error"]) == ([], [], 0, None)


def test_status_of_a_line_that_cannot_run_carries_the_reason(client: TestClient, broken_line):
    body = client.get(f"{API}/sources/phone").json()
    assert body["configured"] is False and body["problem"] == "VOICECRM_PHONE_ACS_ENDPOINT is not set"


# ---- browser tokens --------------------------------------------------------------------------------------------------
def test_a_caller_gets_a_token_for_the_dialer(client: TestClient, phone):
    response = client.post(f"{API}/phone/token", json={
        "role": "caller", "display_name": "Dana", "profile": {"name": "Dana Whitfield", "org": "Harborview Pension Fund"}})
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["token"] == "fake-token" and body["identity"].startswith("8:acs:") and body["display_name"] == "Dana"
    assert (body["line_identity"], body["number"]) == (LINE, "+1 800 555 0100") and "expires_on" in body
    assert phone.tel.ops("issue_token") == [((None,), {})]


def test_tokens_are_refused_when_the_line_is_off(client: TestClient):
    response = client.post(f"{API}/phone/token", json={"role": "caller"})
    assert response.status_code == 409 and "VOICECRM_PHONE_MODE=acs" in response.json()["detail"]


def test_tokens_are_refused_with_the_reason_when_the_line_is_misconfigured(client: TestClient, broken_line):
    response = client.post(f"{API}/phone/token", json={"role": "seller"})
    assert response.status_code == 409 and "ACS_ENDPOINT" in response.json()["detail"]


def test_the_line_identity_cannot_be_borrowed_for_a_browser_call(client: TestClient, phone):
    response = client.post(f"{API}/phone/token", json={"role": "seller", "identity": LINE})
    assert response.status_code == 422 and "cannot be used" in response.json()["detail"]
    assert not phone.tel.ops("issue_token")


@pytest.mark.parametrize("body", [{"role": "admin"}, {}, {"role": "caller", "display_name": "x" * 121},
                                  {"role": "caller", "profile": {"email": "e" * 255}}])
def test_token_requests_are_validated(client: TestClient, phone, body):
    assert client.post(f"{API}/phone/token", json=body).status_code == 422


@pytest.mark.parametrize("error,status", [(TelephonyError("the identity API refused the credential", status=403), 502),
                                          (TelephonyError("not configured"), 409)])
def test_service_errors_become_gateway_or_configuration_errors(client: TestClient, phone, error, status):
    phone.tel.fail["issue_token"] = error
    response = client.post(f"{API}/phone/token", json={"role": "caller"})
    assert response.status_code == status and error.args[0] in response.json()["detail"]


# ---- the seller console ----------------------------------------------------------------------------------------------
def test_a_browser_registers_and_unregisters_as_the_seller_console(client: TestClient, phone):
    response = client.post(f"{API}/phone/seller", json={"identity": CONSOLE, "name": "Aisha West", "email": "aisha@contoso.example"})
    assert response.json() == {"registered": True, "ttl_seconds": 45}
    assert client.get(f"{API}/sources/phone").json()["seller"] == {"mode": "console", "name": "Aisha West", "online": True}
    assert client.delete(f"{API}/phone/seller", params={"identity": CONSOLE}).json()["registered"] is False
    assert client.get(f"{API}/sources/phone").json()["seller"]["mode"] == "none"


def test_the_seller_console_needs_the_line_and_an_identity(client: TestClient, phone):
    assert client.post(f"{API}/phone/seller", json={"identity": ""}).status_code == 422
    assert client.delete(f"{API}/phone/seller").status_code == 422


def test_the_seller_console_is_refused_when_the_line_is_off(client: TestClient):
    assert client.post(f"{API}/phone/seller", json={"identity": CONSOLE}).status_code == 409
    assert client.delete(f"{API}/phone/seller", params={"identity": CONSOLE}).status_code == 409


# ---- webhooks --------------------------------------------------------------------------------------------------------
def test_the_health_endpoint_says_which_door_this_is(ingress: TestClient, phone):
    assert ingress.get(HEALTH).json() == {"service": "voicecrm-phone", "ok": True, "line": True}


def test_the_health_endpoint_answers_without_a_line_too(ingress: TestClient):
    assert ingress.get(HEALTH).json() == {"service": "voicecrm-phone", "ok": True, "line": False}


def test_events_are_refused_without_the_secret_key(ingress: TestClient, phone, caplog):
    caplog.set_level(logging.DEBUG)
    for url in (EVENTS, f"{EVENTS}?key=wrong-key-attempt", f"{EVENTS}?key="):
        assert ingress.post(url, json=EVENT_GRID_HANDSHAKE).status_code == 401           # even the handshake
        assert ingress.options(url).status_code == 401
    ours = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("voicecrm"))
    assert "refused" in ours and "wrong-key-attempt" not in ours and SECRET not in ours     # the log never holds the key
    assert not phone.tel.calls


def test_the_event_grid_handshake_is_answered_with_its_code(ingress: TestClient, phone):
    response = ingress.post(f"{EVENTS}?key={SECRET}", json=EVENT_GRID_HANDSHAKE)
    assert response.status_code == 200 and response.json() == {"validationResponse": "abc-123"}


def test_cloud_event_delivery_is_allowed_for_the_origin_that_asks(ingress: TestClient, phone):
    response = ingress.options(f"{EVENTS}?key={SECRET}", headers={"WebHook-Request-Origin": "eventgrid.azure.net"})
    assert response.status_code == 200 and response.headers["webhook-allowed-origin"] == "eventgrid.azure.net"
    assert "POST" in response.headers["allow"]


def test_an_incoming_call_event_is_accepted_and_the_call_answered(ingress: TestClient, phone):
    response = ingress.post(f"{EVENTS}?key={SECRET}", json=[incoming_event(caller=CALLER)])
    assert response.status_code == 200 and response.json() == {"status": "accepted", "events": 1}
    ((args, kw),) = wait_for(lambda: phone.tel.ops("answer"))
    assert args[0] == "ctx-1" and args[1].startswith("https://line.example.test/api/v1/webhooks/phone/calls/corr-1?t=")
    assert [call.caller for call in phone.line.status().active] == ["Dana Whitfield"]


def test_a_single_cloud_event_object_is_accepted_too(ingress: TestClient, phone):
    response = ingress.post(f"{EVENTS}?key={SECRET}", json=recording_event())
    assert response.status_code == 200 and response.json()["events"] == 1


def test_an_unreadable_body_is_rejected(ingress: TestClient, phone):
    response = ingress.post(f"{EVENTS}?key={SECRET}", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_an_oversized_body_is_rejected_before_it_is_read(ingress: TestClient, phone):
    big = b"[" + b" " * 1_000_001 + b"]"
    for url in (f"{EVENTS}?key={SECRET}", f"{API}/webhooks/phone/calls/corr-1?t={wire.callback_token(SECRET, 'corr-1')}"):
        assert ingress.post(url, content=big, headers={"content-type": "application/json"}).status_code == 413


def test_callbacks_need_the_token_of_that_call(ingress: TestClient, phone):
    ingress.post(f"{EVENTS}?key={SECRET}", json=[incoming_event()])
    wait_for(lambda: phone.tel.ops("answer"))
    body = [callback_event("CallConnected", "conn-1")]
    good = wire.callback_token(SECRET, "corr-1")
    for url in (f"{API}/webhooks/phone/calls/corr-1", f"{API}/webhooks/phone/calls/corr-1?t=nope",
                f"{API}/webhooks/phone/calls/corr-2?t={good}",                                     # another call's token
                f"{API}/webhooks/phone/calls/corr-1?t={wire.callback_token('another-secret', 'corr-1')}"):
        assert ingress.post(url, json=body).status_code == 401
    assert not phone.tel.ops("play_text")


def test_callbacks_with_the_right_token_drive_the_call(ingress: TestClient, phone):
    ingress.post(f"{EVENTS}?key={SECRET}", json=[incoming_event()])
    wait_for(lambda: phone.tel.ops("answer"))
    conn = phone.tel.answered[0].call_connection_id
    wait_for(lambda: phone.line.status().active and phone.line.status().active[0].state == "connecting")
    response = ingress.post(f"{API}/webhooks/phone/calls/corr-1?t={wire.callback_token(SECRET, 'corr-1')}",
                            json=[callback_event("CallConnected", conn)])
    assert response.status_code == 200 and response.json() == {"status": "accepted", "events": 1}
    ((args, kw),) = wait_for(lambda: phone.tel.ops("play_text"))                                # the notice is the next step
    assert args[0] == conn and kw["context"] == "notice"


def test_callbacks_for_unknown_calls_are_accepted_and_ignored(ingress: TestClient, phone):
    response = ingress.post(f"{API}/webhooks/phone/calls/ghost?t={wire.callback_token(SECRET, 'ghost')}",
                            json=[callback_event("CallConnected", "conn-9")])
    assert response.status_code == 200 and phone.line.status().calls == 0


def test_the_webhooks_say_so_when_the_line_is_off(ingress: TestClient):
    assert ingress.post(f"{EVENTS}?key={SECRET}", json=[]).status_code == 503
    assert ingress.post(f"{API}/webhooks/phone/calls/x?t=y", json=[]).status_code == 503


# ---- the ingress is a small door -------------------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", [
    ("get", f"{API}/sources/phone"), ("post", f"{API}/phone/token"), ("post", f"{API}/phone/seller"),
    ("get", f"{API}/conversations"), ("get", f"{API}/metrics"), ("get", "/docs"), ("get", "/redoc"), ("get", "/openapi.json"),
    ("get", "/"), ("get", f"{API}/health")])
def test_the_ingress_serves_nothing_but_the_phone_webhooks(ingress: TestClient, phone, method, path):
    assert getattr(ingress, method)(path).status_code == 404


def test_the_ingress_has_exactly_the_webhook_routes(api_container):
    paths = sorted(create_ingress_app(api_container).openapi()["paths"])          # the schema is generated, never served
    assert paths == [f"{API}/webhooks/phone/calls/{{call_id}}", f"{API}/webhooks/phone/events", f"{API}/webhooks/phone/health"]


# ---- a real listener ---------------------------------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_ingress_listener_serves_the_webhooks_on_its_own_port(api_container, phone):
    port = free_port()
    server = IngressServer(create_ingress_app(api_container), host="127.0.0.1", port=port)
    assert await server.start() is True
    try:
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{port}{HEALTH}")
            assert health.json()["service"] == "voicecrm-phone"
            assert (await client.get(f"http://127.0.0.1:{port}/docs")).status_code == 404
            handshake = await client.post(f"http://127.0.0.1:{port}{EVENTS}?key={SECRET}", json=EVENT_GRID_HANDSHAKE)
            assert handshake.json() == {"validationResponse": "abc-123"}
    finally:
        await server.stop()
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}{HEALTH}")


async def test_a_port_that_is_already_taken_is_reported_without_taking_the_app_down(api_container, caplog, main_server_logging):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        server = IngressServer(create_ingress_app(api_container), host="127.0.0.1", port=taken.getsockname()[1])
        assert await server.start(wait_seconds=5) is False
        await server.stop()
    assert "could not start" in caplog.text
    assert any("error while attempting to bind" in m for m in main_server_logging.messages)      # the reason still shows


async def test_the_container_starts_and_stops_the_ingress_with_the_line(api_container, tmp_path):
    port = free_port()
    c = api_container.isolated()                       # its own store and CRM: shutting it down leaves the shared container alone
    c.settings = phone_settings(tmp_path, phone_ingress_port=port)
    c.phone = PhoneLine(c.service, FakeTelephony(), settings=c.settings, sleep=Clock().sleep)
    await c.startup()
    try:
        assert c.phone.status().ingress_listening is True
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{port}{HEALTH}")).json()["line"] is True
    finally:
        await c.shutdown()
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}{HEALTH}")


async def test_the_app_starts_without_the_ingress_when_its_port_is_busy_or_disabled(api_container, tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        for port, listening in ((taken.getsockname()[1], False), (0, False)):
            c = api_container.isolated()
            c.settings = phone_settings(tmp_path, phone_ingress_port=port)
            c.phone = PhoneLine(c.service, FakeTelephony(), settings=c.settings, sleep=Clock().sleep)
            await c.startup()
            try:
                assert c.phone.status().ingress_listening is listening
            finally:
                await c.shutdown()


# ---- the ingress must not disturb the main server ---------------------------------------------------------------------
UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture()
def main_server_logging() -> Iterator[Captured]:
    """What uvicorn sets up for the main server: INFO on its loggers, and a handler that writes what they say."""
    loggers = [logging.getLogger(name) for name in UVICORN_LOGGERS]
    saved = [(lg.level, list(lg.handlers), lg.propagate, list(lg.filters)) for lg in loggers]
    captured = Captured()
    for lg in loggers:
        lg.setLevel(logging.INFO)
        lg.addHandler(captured)
    yield captured
    for lg, (level, handlers, propagate, filters) in zip(loggers, saved, strict=True):
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.filters = filters


def logging_state() -> list[tuple[int, int, bool]]:
    return [(lg.level, len(lg.handlers), lg.propagate) for lg in map(logging.getLogger, UVICORN_LOGGERS)]


async def serving(server: uvicorn.Server, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        await asyncio.sleep(0.02)


def test_creating_the_ingress_leaves_uvicorns_shared_loggers_alone(api_container, main_server_logging):
    before = logging_state()
    IngressServer(create_ingress_app(api_container), host="127.0.0.1", port=0)
    assert logging_state() == before                  # a changed level or handler would silence the main server's own output


async def test_the_ingress_never_writes_an_access_line_but_a_normal_server_does(api_container, main_server_logging):
    port = free_port()
    ingress = IngressServer(create_ingress_app(api_container), host="127.0.0.1", port=port)
    assert await ingress.start() is True
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"http://127.0.0.1:{port}{EVENTS}?key={SECRET}", json=EVENT_GRID_HANDSHAKE)
            await client.get(f"http://127.0.0.1:{port}{HEALTH}")
    finally:
        await ingress.stop()
    heard = " ".join(main_server_logging.messages)
    assert SECRET not in heard and "HTTP/1.1" not in heard          # the secret is in the URL: nothing may print it

    plain_port = free_port()                                          # the same app on a server like the main one: it logs requests
    plain = _EmbeddedServer(uvicorn.Config(create_ingress_app(api_container), host="127.0.0.1", port=plain_port, lifespan="off",
                                           log_config=None))
    task = asyncio.create_task(plain.serve())
    try:
        await serving(plain)
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{plain_port}{HEALTH}")
    finally:
        plain.should_exit = True
        await task
    assert any("HTTP/1.1" in message for message in main_server_logging.messages)          # so the check above is not vacuous


async def test_the_main_servers_startup_messages_survive_an_ingress_started_in_its_lifespan(api_container, main_server_logging):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ingress = IngressServer(create_ingress_app(api_container), host="127.0.0.1", port=0)
        await ingress.start()
        yield
        await ingress.stop()

    main = _EmbeddedServer(uvicorn.Config(FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, log_config=None))
    task = asyncio.create_task(main.serve())
    try:
        await serving(main)
    finally:
        main.should_exit = True
        await task
    messages = main_server_logging.messages
    # without this line the console stops at "Waiting for application startup." and the app looks hung
    assert "Application startup complete." in messages
    # the ingress is a second server in the same process, but the console must not read as if two apps had started
    assert sum(m.startswith("Started server process") for m in messages) == 1
    assert sum(m.startswith("Uvicorn running on") for m in messages) == 1
    assert sum(m == "Shutting down" for m in messages) == 1 and sum(m.startswith("Finished server process") for m in messages) == 1
    assert [m for m in messages if m.startswith("Phone ingress listening on")] == [
        "Phone ingress listening on http://127.0.0.1:0 (tunnel this port; it serves only the phone webhooks)"]

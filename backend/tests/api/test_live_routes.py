"""Live demo routes: the load test (estimate, start, poll, stop, remove) and the in-app service check."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from .helpers import API

DONE = ("finished", "stopped", "failed")
ACTIVE = ("preparing", "running", "stopping")


def wait_until_done(client: TestClient, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        status = client.get(f"{API}/live/load").json()
        if status["state"] in DONE:
            return status
        time.sleep(0.05)
    client.post(f"{API}/live/load/stop")
    pytest.fail(f"the load test was still {status.get('state')} after {timeout} s")


@pytest.fixture(autouse=True)
def clean(client: TestClient):
    yield
    client.post(f"{API}/live/load/stop")
    deadline = time.monotonic() + 60
    while client.get(f"{API}/live/load").json()["state"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(0.05)
    client.delete(f"{API}/live/load/calls")


def test_status_when_idle_states_the_limits(client: TestClient):
    body = client.get(f"{API}/live/load").json()
    assert body["state"] == "idle" and body["report"] is None and body["max_calls"] == 2000
    assert body["pipeline"]["submitted"] >= 0 and body["stored_calls"] == 0


def test_estimate_prices_the_run_before_it_starts(client: TestClient):
    body = client.post(f"{API}/live/load/estimate", json={"count": 800, "via": "transcript"}).json()
    assert body["calls"] == 800 and body["billed"] == [] and body["cost_usd"] == 0 and body["parallel"] == 16
    assert body["pool"] == len(client.get(f"{API}/fixtures").json()) and body["seconds"] > 0


@pytest.mark.parametrize("body,status,fragment", [
    ({"count": 2001}, 422, "limited to 2,000"),
    ({"count": 0}, 422, "greater than or equal to 1"),
    ({"count": 5, "via": "carrier-pigeon"}, 422, "via"),
    ({"count": 5, "surprise": True}, 422, "surprise"),
    ({"count": 5, "fixtures": ["nope"]}, 422, "unknown calls"),
])
def test_bad_requests_are_refused_with_the_reason(client: TestClient, body, status, fragment):
    for path in ("/live/load/estimate", "/live/load"):
        response = client.post(f"{API}{path}", json=body)
        assert response.status_code == status and fragment in response.text
    assert client.get(f"{API}/live/load").json()["state"] == "idle"


def test_a_run_is_started_polled_listed_in_the_inbox_and_removed(client: TestClient):
    response = client.post(f"{API}/live/load", json={"count": 6, "parallel": 3})
    assert response.status_code == 202 and response.json()["state"] in ("preparing", "running")
    final = wait_until_done(client)
    assert final["state"] == "finished" and final["report"]["calls"] == 6 and final["report"]["failed"] == 0
    assert final["stored_calls"] == 6 and final["report"]["pipeline"]["completed"] == 6
    inbox = client.get(f"{API}/conversations", params={"limit": 50}).json()
    tagged = [item for item in inbox if item["batch_id"]]
    assert len(tagged) == 6 and {item["batch_id"] for item in tagged} == {f"load-{final['run_id']}"}
    assert client.delete(f"{API}/live/load/calls").json() == {"deleted": 6}
    assert client.get(f"{API}/live/load").json()["stored_calls"] == 0
    assert [item for item in client.get(f"{API}/conversations", params={"limit": 50}).json() if item["batch_id"]] == []


def test_a_second_start_while_running_is_a_conflict_and_stop_ends_the_run(client: TestClient):
    assert client.post(f"{API}/live/load", json={"count": 300, "rate": 15, "parallel": 2}).status_code == 202
    deadline = time.monotonic() + 30
    while client.get(f"{API}/live/load").json()["sent"] < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    again = client.post(f"{API}/live/load", json={"count": 1})
    assert again.status_code == 409 and "already running" in again.json()["detail"]
    assert client.delete(f"{API}/live/load/calls").status_code == 409
    assert client.post(f"{API}/live/load/stop").json()["state"] == "stopping"
    final = wait_until_done(client)
    assert final["state"] == "stopped" and final["report"]["stopped_early"] and final["sent"] < 300


def test_config_names_the_model_deployment_only_when_the_model_is_live(client: TestClient):
    assert client.get(f"{API}/config").json()["llm_deployment"] is None


# ---- service check ---------------------------------------------------------------------------------------------------
def test_the_service_check_probes_what_is_running_and_scores_each_scenario(client: TestClient):
    body = client.post(f"{API}/live/preflight", json={"only": ["crm"]}).json()
    names = {check["name"]: check for check in body["checks"]}
    assert names["crm: health"]["status"] == "pass" and "simulator" in names["crm: health"]["detail"]
    assert [item["scenario"][:1] for item in body["readiness"]] == ["A", "B", "C", "D"]
    assert all(item["verdict"] in ("READY", "NOT READY", "NOT CONFIGURED") for item in body["readiness"])
    assert body["seconds"] >= 0
    assert client.post(f"{API}/live/preflight", json={}).status_code == 200


def test_the_teams_check_says_how_to_turn_teams_on_when_it_is_off(client: TestClient):
    body = client.post(f"{API}/live/preflight", json={"only": ["teams"]}).json()
    assert [(c["name"], c["status"]) for c in body["checks"]] == [("teams: Graph access", "skip")]
    assert "VOICECRM_TEAMS_MODE=poll" in body["checks"][0]["hint"]


def test_the_phone_check_says_how_to_turn_the_line_on_when_it_is_off(client: TestClient):
    body = client.post(f"{API}/live/preflight", json={"only": ["phone"]}).json()
    assert [(c["name"], c["status"]) for c in body["checks"]] == [("phone: line", "skip")]
    assert "VOICECRM_PHONE_MODE=acs" in body["checks"][0]["hint"]
    assert body["readiness"][3]["verdict"] == "NOT CONFIGURED" and "docs/phone-line.md" in body["readiness"][3]["why"]


def test_unknown_probes_are_rejected(client: TestClient):
    assert client.post(f"{API}/live/preflight", json={"only": ["coffee"]}).status_code == 422

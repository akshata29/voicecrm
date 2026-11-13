"""Reset the demo: what it removes, what it keeps, when it refuses, and that the next run starts clean."""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecrm.container import Container
from voicecrm.pipeline.service import PipelineService

from .helpers import API, approve, crm_rows, ingest_fixture, scripted_calls

CALLS = scripted_calls()
CLEAN = "pb-teams-equity-class-c"                 # one product, two seller tasks
AMBIGUOUS = "pb-ambiguous-equity-fund"            # share class never said: waits for a pick
LEGACY = "pb-teams-legacy-blue-chip"              # a former fund name: needs one pick, then it is learned
ACTIVE = ("preparing", "running", "stopping")
ZERO = {"conversations": 0, "crm_phone_calls": 0, "crm_tasks": 0, "crm_insights": 0, "vault_items": 0, "learned_aliases": 0,
        "evaluation_runs": 0, "policy_changes": False}


def counts(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in ZERO}


def settle_load_test(client: TestClient) -> None:
    client.post(f"{API}/live/load/stop")
    deadline = time.monotonic() + 60
    while client.get(f"{API}/live/load").json()["state"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(0.05)


def reset(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{API}/demo/reset", json={"confirm": True})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(autouse=True)
def clean(client: TestClient):
    settle_load_test(client)
    reset(client)
    yield
    settle_load_test(client)
    reset(client)


def seed_demo(client: TestClient, api_container: Container) -> None:
    """One call written to the CRM, one waiting for a pick, a learned alias, a scoreboard run and a policy change."""
    approve(client, ingest_fixture(client, CLEAN)[0])
    ingest_fixture(client, AMBIGUOUS)
    pending, detail = ingest_fixture(client, LEGACY)
    mention = detail["record"]["proposal"]["result"]["products"][0]
    approve(client, pending, {"product_choices": {mention["mention_id"]: CALLS[LEGACY].gold.products[0].product_id}})
    assert client.post(f"{API}/eval/run", json={"engine": "rules"}).status_code == 200
    processing = client.get(f"{API}/policies").json()["processing"]
    assert client.put(f"{API}/policies/processing", json={**processing, "auto_approve_enabled": True}).status_code == 200


# ---- the preview -------------------------------------------------------------------------------------------------------
def test_an_empty_demo_has_nothing_to_reset(client: TestClient):
    body = client.get(f"{API}/demo/data").json()
    assert counts(body) == ZERO
    assert body["can_reset"] is True and body["reason"] is None and body["in_flight"] == 0 and body["crm"] == "local_sim"


def test_the_preview_counts_what_a_reset_would_remove(client: TestClient, api_container: Container):
    seed_demo(client, api_container)
    body = client.get(f"{API}/demo/data").json()
    assert body["conversations"] == 3 and body["learned_aliases"] == 1 and body["evaluation_runs"] == 1
    assert body["policy_changes"] is True
    written = {entity: len(crm_rows(client, entity)) for entity in ("phonecall", "task", "insight")}
    assert written["phonecall"] == 2 and written["task"] >= 2
    assert (body["crm_phone_calls"], body["crm_tasks"], body["crm_insights"]) == (written["phonecall"], written["task"], written["insight"])
    assert body["vault_items"] == len(client.get(f"{API}/vault/items").json()) and body["vault_items"] >= 1       # the pending calls
    assert client.get(f"{API}/demo/data").json() == body                                                         # looking removes nothing


# ---- the reset ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [{}, {"confirm": False}])
def test_a_reset_needs_an_explicit_confirmation(client: TestClient, api_container: Container, body: dict):
    seed_demo(client, api_container)
    before = client.get(f"{API}/demo/data").json()
    response = client.post(f"{API}/demo/reset", json=body)
    assert response.status_code == 409 and "confirmation required" in response.json()["detail"]
    assert client.get(f"{API}/demo/data").json() == before


def test_a_reset_removes_everything_the_app_stored_and_reports_what_it_removed(client: TestClient, api_container: Container):
    seed_demo(client, api_container)
    before = client.get(f"{API}/demo/data").json()
    reference = {entity: crm_rows(client, entity) for entity in ("account", "contact", "opportunity")}
    assert all(reference.values())

    removed = reset(client)
    assert removed == counts(before)

    assert client.get(f"{API}/conversations", params={"limit": 100}).json() == []
    summary = client.get(f"{API}/metrics/summary").json()
    assert summary["conversations"] == 0 and summary["written"] == 0 and summary["needs_review"] == 0
    assert all(crm_rows(client, entity) == [] for entity in ("phonecall", "task", "insight"))
    assert client.get(f"{API}/vault/items").json() == []
    assert client.get(f"{API}/aliases").json() == [] and client.get(f"{API}/catalogue/stats").json()["learned_aliases"] == 0
    assert client.get(f"{API}/eval/runs").json() == []
    assert client.get(f"{API}/policies").json()["processing"]["auto_approve_enabled"] is False
    assert counts(client.get(f"{API}/demo/data").json()) == ZERO


def test_a_reset_keeps_the_reference_data_and_the_next_run_starts_clean(client: TestClient, api_container: Container):
    reference = {entity: crm_rows(client, entity) for entity in ("account", "contact", "opportunity")}
    seed_demo(client, api_container)
    products = client.get(f"{API}/catalogue/stats").json()["products"]
    reset(client)
    assert {entity: crm_rows(client, entity) for entity in reference} == reference           # accounts, contacts, opportunities
    assert client.get(f"{API}/catalogue/stats").json()["products"] == products
    assert len(client.get(f"{API}/templates").json()) >= 2 and client.get(f"{API}/taxonomy").json()["topics"]
    assert client.get(f"{API}/fixtures").status_code == 200

    conversation_id, detail = ingest_fixture(client, CLEAN)                                  # the pipeline still works
    written = approve(client, conversation_id)
    assert written["record"]["status"] == "written" and len(crm_rows(client, "phonecall", conversation_id)) == 1
    assert detail["record"]["proposal"]["crm_match"]["account"] is not None                  # and still matches the seed


def test_a_learned_alias_is_forgotten_by_the_resolver_not_only_by_the_table(client: TestClient, api_container: Container):
    gold = CALLS[LEGACY].gold.products[0].product_id
    pending, detail = ingest_fixture(client, LEGACY)
    mention = detail["record"]["proposal"]["result"]["products"][0]
    assert mention["chosen_product_id"] is None and mention["needs_review"] is True
    approve(client, pending, {"product_choices": {mention["mention_id"]: gold}})

    _, learned = ingest_fixture(client, LEGACY)
    assert learned["record"]["proposal"]["result"]["products"][0]["chosen_product_id"] == gold

    assert reset(client)["learned_aliases"] == 1
    _, forgotten = ingest_fixture(client, LEGACY)
    again = forgotten["record"]["proposal"]["result"]["products"][0]
    assert again["chosen_product_id"] is None and again["needs_review"] is True


def test_a_reset_clears_the_phone_lines_finished_calls_too(client: TestClient, api_container: Container, monkeypatch):
    class Line:
        forgotten = 0

        def forget(self) -> None:
            Line.forgotten += 1

    monkeypatch.setattr(api_container, "phone", Line())
    reset(client)
    assert Line.forgotten == 1


def test_the_figures_that_describe_the_removed_calls_are_cleared_too(client: TestClient):
    assert client.post(f"{API}/live/load", json={"count": 6, "parallel": 3}).status_code == 202
    deadline = time.monotonic() + 60
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = client.get(f"{API}/live/load").json()
        if status["state"] == "finished":
            break
        time.sleep(0.05)
    assert status["state"] == "finished" and status["report"]["calls"] == 6 and status["stored_calls"] == 6
    assert client.get(f"{API}/metrics/pipeline").json()["completed"] >= 6

    assert reset(client)["conversations"] == 6
    after = client.get(f"{API}/live/load").json()
    assert after["state"] == "idle" and after["report"] is None and after["stored_calls"] == 0 and after["sent"] == 0
    pipeline = client.get(f"{API}/metrics/pipeline").json()
    assert (pipeline["submitted"], pipeline["completed"], pipeline["lifetime_completed"]) == (0, 0, 0)


# ---- when it refuses ----------------------------------------------------------------------------------------------------
def test_it_waits_for_calls_that_are_still_being_processed(client: TestClient, api_container: Container, monkeypatch):
    ingest_fixture(client, CLEAN)
    monkeypatch.setattr(PipelineService, "in_flight", property(lambda self: 2))
    body = client.get(f"{API}/demo/data").json()
    assert body["can_reset"] is False and body["in_flight"] == 2 and "2 calls are still being processed" in body["reason"]
    response = client.post(f"{API}/demo/reset", json={"confirm": True})
    assert response.status_code == 409 and "still being processed" in response.json()["detail"]
    monkeypatch.undo()
    assert client.get(f"{API}/demo/data").json()["conversations"] == 1                       # nothing was removed


def test_it_waits_for_a_running_load_test(client: TestClient):
    assert client.post(f"{API}/live/load", json={"count": 300, "rate": 15, "parallel": 2}).status_code == 202
    deadline = time.monotonic() + 30
    while client.get(f"{API}/live/load").json()["sent"] < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        body = client.get(f"{API}/demo/data").json()
        assert body["can_reset"] is False and "load test is running" in body["reason"]
        response = client.post(f"{API}/demo/reset", json={"confirm": True})
        assert response.status_code == 409 and "load test is running" in response.json()["detail"]
    finally:
        settle_load_test(client)
    assert client.get(f"{API}/demo/data").json()["conversations"] >= 2                       # the run's calls are still there


def test_it_never_runs_against_a_real_crm(client: TestClient, api_container: Container, monkeypatch):
    ingest_fixture(client, CLEAN)

    class RealCrm:                                    # a Dataverse-like adapter: it cannot clear what it wrote
        name = "dataverse"

        async def health(self) -> dict[str, Any]:
            return {"adapter": "dataverse", "ok": True}

    monkeypatch.setattr(api_container.deps, "crm", RealCrm())
    body = client.get(f"{API}/demo/data").json()
    assert body["can_reset"] is False and body["crm"] == "dataverse" and "real Dataverse" in body["reason"]
    response = client.post(f"{API}/demo/reset", json={"confirm": True})
    assert response.status_code == 409 and "real Dataverse" in response.json()["detail"]
    monkeypatch.undo()
    assert client.get(f"{API}/demo/data").json()["conversations"] == 1                       # nothing was removed

"""GET /metrics/pipeline: the live figures someone watches while a batch of calls is processed."""
from __future__ import annotations

from fastapi.testclient import TestClient

from .helpers import API, scripted_calls, start_ingest, wait_for_status

KEYS = {"active", "started_at", "max_concurrency", "submitted", "queued", "in_flight", "completed", "failed",
        "elapsed_seconds", "calls_per_minute", "recent_calls_per_minute", "eta_seconds", "queue_wait_seconds",
        "end_to_end_seconds", "stages_ms", "tokens_in", "tokens_out", "cost_usd", "cost_illustrative", "engines",
        "fallbacks", "errors", "lifetime_completed", "lifetime_failed"}


def pipeline(client: TestClient) -> dict:
    response = client.get(f"{API}/metrics/pipeline")
    assert response.status_code == 200, response.text
    return response.json()


def test_an_idle_pipeline_reports_an_empty_batch(client: TestClient):
    body = pipeline(client)
    assert set(body) == KEYS
    assert (body["active"], body["submitted"], body["queued"], body["in_flight"]) == (False, 0, 0, 0)
    assert body["max_concurrency"] > 0 and body["cost_illustrative"] is True


def test_a_burst_of_calls_is_reported_as_one_batch(client: TestClient):
    fixtures = list(scripted_calls())[:6]
    ids = [start_ingest(client, fixture_id) for fixture_id in fixtures]
    for conversation_id in ids:
        wait_for_status(client, conversation_id)
    body = pipeline(client)
    assert (body["submitted"], body["completed"], body["failed"]) == (6, 6, 0)
    assert (body["queued"], body["in_flight"], body["active"]) == (0, 0, False)
    assert body["end_to_end_seconds"]["count"] == 6 and body["end_to_end_seconds"]["p95"] is not None
    assert {"ingest", "normalize", "policy", "extract", "resolve", "validate"} <= set(body["stages_ms"])
    assert sum(body["engines"].values()) == 6 and body["errors"] == []
    assert body["lifetime_completed"] == 6 and body["started_at"] is not None

"""Operational metrics (GET /metrics/summary) and the evaluation scoreboard (POST /eval/run, GET /eval/runs[/id])."""
from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .helpers import API, approve, crm_rows, ingest_fixture, scripted_calls, start_ingest, wait_for_status

CALLS = scripted_calls()
CLEAN = "pb-teams-equity-class-c"
AMBIGUOUS = "pb-ambiguous-equity-fund"
GENESYS = "pb-genesys-inbound-service"
SUMMARY_KEYS = {"conversations", "by_status", "by_channel", "written", "needs_review", "auto_approved",
                "latency_p50_seconds", "latency_p95_seconds", "mean_cost_usd", "mean_clicks_to_approve",
                "mean_seconds_to_review", "cost_illustrative"}
METRIC_KEYS = {"cases", "product_top1", "product_top3", "product_false_tag_rate", "share_class_confusion_rate",
               "topic_precision", "topic_recall", "topic_f1", "task_precision", "task_recall", "privacy_leaks",
               "relationship_kept_rate", "latency_p50_ms", "latency_p95_ms", "mean_cost_usd", "auto_approvable_rate"}
RATIOS = ("product_top1", "product_top3", "product_false_tag_rate", "share_class_confusion_rate", "topic_precision",
          "topic_recall", "topic_f1", "task_precision", "task_recall", "relationship_kept_rate", "auto_approvable_rate")


def snapshot(client: TestClient) -> dict[str, int]:
    """Everything an evaluation run must NOT touch: conversations, CRM rows, vault items, learned aliases."""
    return {
        "conversations": len(client.get(f"{API}/conversations", params={"limit": 500}).json()),
        "vault": len(client.get(f"{API}/vault/items").json()),
        "aliases": len(client.get(f"{API}/aliases").json()),
        **{entity: len(crm_rows(client, entity)) for entity in ("phonecall", "task", "insight")},
    }


def summary(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{API}/metrics/summary")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module", autouse=True)
def initial_summary(client: TestClient) -> dict[str, Any]:
    """Captured before any test of this module touches the app."""
    return summary(client)


@pytest.fixture(scope="module")
def baseline(client: TestClient, initial_summary: dict[str, Any]) -> dict[str, Any]:
    before = snapshot(client)
    response = client.post(f"{API}/eval/run", json={})
    assert response.status_code == 200, response.text
    return {"run": response.json(), "before": before, "after": snapshot(client)}


@pytest.fixture(scope="module")
def noisy(client: TestClient, baseline: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"{API}/eval/run", json={"with_asr_noise": True, "label": "noise run"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def reviewed(client: TestClient, initial_summary: dict[str, Any]) -> dict[str, str]:
    """Two approved conversations (1 and 3 clicks, two channels) and one left pending."""
    first, _ = ingest_fixture(client, CLEAN)
    second, _ = ingest_fixture(client, GENESYS)
    pending, _ = ingest_fixture(client, AMBIGUOUS)
    approve(client, first, {"clicks": 1})
    approve(client, second, {"clicks": 3})
    return {"approved_1": first, "approved_3": second, "pending": pending}


# ---- evaluation scoreboard ------------------------------------------------------------------------------------------------
def test_eval_run_scores_every_scripted_call(client: TestClient, baseline: dict[str, Any]) -> None:
    run = baseline["run"]
    assert run["run_id"] and run["created_at"]
    assert (run["engine"], run["with_asr_noise"], run["label"], run["learned_aliases"]) == ("rules", False, "baseline", 0)

    cases = run["cases"]
    assert len(cases) >= 16
    assert run["metrics"]["cases"] == len(cases) == len(CALLS)
    assert {c["fixture_id"] for c in cases} == set(CALLS) == {f["fixture_id"] for f in client.get(f"{API}/fixtures").json()}
    for case in cases:
        gold = CALLS[case["fixture_id"]].gold
        assert case["product_gold"] == len(gold.products)
        assert case["kept_relationship_gold"] == len(gold.must_keep)
        assert case["details"]["status"] == "needs_review", case["fixture_id"]
        assert case["details"]["template"] == gold.template_id
        assert case["details"]["crm_ok"] is True, case["fixture_id"]
        assert case["latency_ms"] is not None and case["cost_usd"] is not None


def test_eval_run_reports_the_scoreboard_metrics(baseline: dict[str, Any]) -> None:
    metrics = baseline["run"]["metrics"]
    assert METRIC_KEYS <= set(metrics)
    assert all(0.0 <= metrics[key] <= 1.0 for key in RATIOS)
    assert metrics["privacy_leaks"] == 0
    assert metrics["latency_p50_ms"] > 0 and metrics["latency_p95_ms"] >= metrics["latency_p50_ms"]
    assert metrics["mean_cost_usd"] >= 0
    totals = Counter()
    for case in baseline["run"]["cases"]:
        totals["top1"] += case["product_top1_correct"]
        totals["leaks"] += case["leaks"]
    assert totals["leaks"] == metrics["privacy_leaks"]
    assert totals["top1"] > 0 and metrics["product_top1"] > 0


def test_eval_run_does_not_touch_live_conversations_crm_vault_or_aliases(baseline: dict[str, Any]) -> None:
    assert baseline["after"] == baseline["before"]


def test_eval_run_accepts_a_label_and_the_asr_noise_switch(noisy: dict[str, Any], baseline: dict[str, Any]) -> None:
    assert (noisy["label"], noisy["with_asr_noise"], noisy["engine"]) == ("noise run", True, "rules")
    assert noisy["run_id"] != baseline["run"]["run_id"]
    assert noisy["metrics"]["cases"] == baseline["run"]["metrics"]["cases"]
    assert len(noisy["cases"]) == len(baseline["run"]["cases"])


def test_eval_runs_are_listed_newest_first_without_case_details(client: TestClient, baseline: dict[str, Any],
                                                                 noisy: dict[str, Any]) -> None:
    runs = client.get(f"{API}/eval/runs").json()
    ids = [r["run_id"] for r in runs]
    assert ids.index(noisy["run_id"]) < ids.index(baseline["run"]["run_id"])
    assert all(r["cases"] == [] and r["metrics"]["cases"] >= 16 for r in runs)
    listed = next(r for r in runs if r["run_id"] == baseline["run"]["run_id"])
    assert listed["metrics"] == baseline["run"]["metrics"]
    assert (listed["label"], listed["engine"]) == ("baseline", "rules")


def test_eval_run_detail_includes_the_cases(client: TestClient, baseline: dict[str, Any]) -> None:
    run = baseline["run"]
    response = client.get(f"{API}/eval/runs/{run['run_id']}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["metrics"] == run["metrics"]
    assert {c["fixture_id"] for c in detail["cases"]} == {c["fixture_id"] for c in run["cases"]}
    assert len(detail["cases"]) == len(run["cases"]) >= 16


def test_unknown_eval_run_is_404(client: TestClient) -> None:
    response = client.get(f"{API}/eval/runs/no-such-run")
    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


@pytest.mark.parametrize("body", [{"engine": "nope"}, {"engine": "llm"}, {"engine": "hybrid"}])
def test_eval_run_rejects_engines_that_are_not_available(client: TestClient, baseline: dict[str, Any],
                                                          body: dict[str, str]) -> None:
    before = len(client.get(f"{API}/eval/runs").json())
    response = client.post(f"{API}/eval/run", json=body)
    assert response.status_code == 422
    assert "not available" in response.json()["detail"] and "rules" in response.json()["detail"]
    assert len(client.get(f"{API}/eval/runs").json()) == before


def test_eval_run_validates_its_body(client: TestClient) -> None:
    assert client.post(f"{API}/eval/run", json={"with_asr_noise": "maybe"}).status_code == 422
    assert client.post(f"{API}/eval/run", json={"engine": 5}).status_code == 422


# ---- operational summary -------------------------------------------------------------------------------------------------
def test_summary_of_an_empty_system(initial_summary: dict[str, Any]) -> None:
    assert set(initial_summary) == SUMMARY_KEYS
    assert initial_summary["conversations"] == 0
    assert (initial_summary["by_status"], initial_summary["by_channel"]) == ({}, {})
    assert (initial_summary["written"], initial_summary["needs_review"], initial_summary["auto_approved"]) == (0, 0, 0)
    for key in ("latency_p50_seconds", "latency_p95_seconds", "mean_cost_usd", "mean_clicks_to_approve",
                "mean_seconds_to_review"):
        assert initial_summary[key] is None, key
    assert initial_summary["cost_illustrative"] is True


def test_summary_counts_conversations_by_status_and_channel(client: TestClient, reviewed: dict[str, str]) -> None:
    result = summary(client)
    items = client.get(f"{API}/conversations", params={"limit": 500}).json()
    assert result["conversations"] == len(items) >= 3
    assert result["by_status"] == dict(Counter(i["status"] for i in items))
    assert result["by_channel"] == dict(Counter(i["source"] for i in items))
    assert result["written"] == result["by_status"]["written"] >= 2
    assert result["needs_review"] == result["by_status"]["needs_review"] >= 1
    assert result["by_channel"]["teams"] >= 2 and result["by_channel"]["genesys"] >= 1
    assert result["auto_approved"] <= result["written"]


def test_summary_averages_reviewer_clicks_seconds_latency_and_cost(client: TestClient,
                                                                   reviewed: dict[str, str]) -> None:
    result = summary(client)
    assert result["mean_clicks_to_approve"] == pytest.approx(2.0)  # the two manual approvals took 1 and 3 clicks
    assert result["mean_seconds_to_review"] is not None and result["mean_seconds_to_review"] >= 0

    items = client.get(f"{API}/conversations", params={"limit": 500}).json()
    latencies = sorted(i["latency_seconds"] for i in items if i["latency_seconds"] is not None)
    assert result["latency_p50_seconds"] == pytest.approx(latencies[round(0.5 * (len(latencies) - 1))], abs=0.01)
    assert result["latency_p95_seconds"] == pytest.approx(latencies[round(0.95 * (len(latencies) - 1))], abs=0.01)
    assert 0 < result["latency_p50_seconds"] <= result["latency_p95_seconds"]
    costs = [i["cost_usd"] for i in items if i["cost_usd"] is not None]
    assert result["mean_cost_usd"] == pytest.approx(sum(costs) / len(costs), abs=1e-5)
    assert result["cost_illustrative"] is True


# ---- auto-approve policy ---------------------------------------------------------------------------------------------------
@pytest.fixture()
def auto_approve(client: TestClient):
    original = client.get(f"{API}/policies").json()["processing"]
    assert client.put(f"{API}/policies/processing", json={**original, "auto_approve_enabled": True}).status_code == 200
    yield
    client.put(f"{API}/policies/processing", json=original)


def test_auto_approve_writes_eligible_calls_without_a_reviewer(client: TestClient, reviewed: dict[str, str],
                                                               auto_approve: None) -> None:
    before = summary(client)
    conversation_id = start_ingest(client, CLEAN)
    detail = wait_for_status(client, conversation_id, statuses=("written", "failed"))
    record = detail["record"]
    assert record["status"] == "written", record["error"]
    review = record["proposal"]["review"]
    assert (review["state"], review["clicks"], review["reviewed_by"]) == ("auto_approved", 0, "auto-approve policy")
    assert record["crm_result"]["ok"] is True
    assert crm_rows(client, "insight", conversation_id)[0]["review_status"] == "auto_approved"

    after = summary(client)
    assert after["auto_approved"] == before["auto_approved"] + 1
    assert after["written"] == before["written"] + 1
    assert after["mean_clicks_to_approve"] == before["mean_clicks_to_approve"]  # only manual approvals count


def test_auto_approve_leaves_calls_that_need_a_product_pick_for_a_human(client: TestClient, auto_approve: None) -> None:
    _, detail = ingest_fixture(client, AMBIGUOUS)
    review = detail["record"]["proposal"]["review"]
    assert detail["record"]["status"] == "needs_review"
    assert review["auto_approve_eligible"] is False and "product_needs_pick" in review["reasons"]

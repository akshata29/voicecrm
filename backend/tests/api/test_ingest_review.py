"""Ingest -> review -> CRM write flow of the HTTP API, driven by the scripted demo calls in data/calls."""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecrm.engines.contracts import REVIEWER_EVIDENCE

from .helpers import (
    API,
    approve,
    contains_leak,
    crm_rows,
    ingest_fixture,
    iter_strings,
    longest_turns,
    norm,
    scripted_calls,
    start_ingest,
    wait_for_status,
)

CALLS = scripted_calls()
CLEAN = "pb-teams-equity-class-c"                 # one product (class stated), two seller tasks, matches by email
AMBIGUOUS = "pb-ambiguous-equity-fund"            # fund named without a share class -> reviewer must pick
GENESYS_PHONE = "pb-genesys-inbound-service"      # CRM contact matched by caller phone number
PRIVACY_FIXTURES = sorted(fid for fid, call in CALLS.items() if call.gold.must_not_leak)
STAGE_ORDER = ["ingest", "transcribe", "normalize", "policy", "extract", "resolve", "validate", "review", "retention",
               "write"]
URI = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$", re.IGNORECASE)
CRM_ENTITIES = ("phonecall", "task", "insight")


# ---- helpers -------------------------------------------------------------------------------------------------------
def _proposal(detail: dict[str, Any]) -> dict[str, Any]:
    return detail["record"]["proposal"]


def _mentions(detail: dict[str, Any]) -> list[dict[str, Any]]:
    return _proposal(detail)["result"]["products"]


def _crm_bundle(client: TestClient, conversation_id: str) -> dict[str, list[dict[str, Any]]]:
    return {entity: crm_rows(client, entity, conversation_id) for entity in CRM_ENTITIES}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _surface_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _assert_no_transcript_text(fixture_id: str, payload: Any) -> None:
    """None of the call's longest turns, and no WebVTT/voice-tag artefacts, may appear anywhere in `payload`."""
    haystacks = [norm(json.dumps(payload, ensure_ascii=False)), norm(" ".join(iter_strings(payload)))]
    for turn in longest_turns(CALLS[fixture_id], 3):
        assert norm(turn) not in haystacks[0] and norm(turn) not in haystacks[1], f"verbatim turn leaked: {turn[:70]!r}"
    for text in iter_strings(payload):
        assert "-->" not in text and "<v " not in text.lower() and not text.lstrip().upper().startswith("WEBVTT")


@pytest.fixture(scope="module")
def pending_clean(client: TestClient) -> tuple[str, dict[str, Any]]:
    """A clean US call left awaiting review. Read-only: no test may approve, reject or reprocess it."""
    return ingest_fixture(client, CLEAN)


@pytest.fixture(scope="module")
def written_us(client: TestClient) -> tuple[str, dict[str, Any]]:
    """A clean US call approved with the one-click path. Read-only."""
    conversation_id, _ = ingest_fixture(client, CLEAN)
    return conversation_id, approve(client, conversation_id)


@pytest.fixture(scope="module")
def pending_ambiguous(client: TestClient) -> tuple[str, dict[str, Any]]:
    """The ambiguous-fund call left awaiting review. Read-only."""
    return ingest_fixture(client, AMBIGUOUS)


@pytest.fixture()
def fresh_clean(client: TestClient) -> str:
    return ingest_fixture(client, CLEAN)[0]


# ---- ingestion ---------------------------------------------------------------------------------------------------
def test_fixtures_endpoint_lists_the_scripted_calls(client: TestClient) -> None:
    fixtures = client.get(f"{API}/fixtures").json()
    assert len(fixtures) >= 12
    assert {f["fixture_id"] for f in fixtures} == set(CALLS)
    for item in fixtures:
        call = CALLS[item["fixture_id"]]
        assert (item["channel"], item["market"], item["turns"]) == (call.channel.value, call.market, len(call.turns))


def test_simulated_ingest_is_asynchronous_and_ends_in_needs_review(client: TestClient) -> None:
    response = client.post(f"{API}/ingest/simulate", json={"fixture_id": CLEAN})
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    assert response.json()["duplicate_of"] is None

    first = client.get(f"{API}/conversations/{conversation_id}").json()["record"]
    assert first["status"] in ("received", "processing", "needs_review")
    detail = wait_for_status(client, conversation_id)
    record = detail["record"]
    assert record["status"] == "needs_review", record["error"]
    assert record["fixture_id"] == CLEAN
    assert record["meta"]["source"] == CALLS[CLEAN].channel.value
    assert record["proposal"]["review"]["state"] == "pending"
    assert record["crm_result"] is None


def test_simulate_validates_its_input(client: TestClient) -> None:
    assert client.post(f"{API}/ingest/simulate", json={"fixture_id": "no-such-call"}).status_code == 404
    assert client.post(f"{API}/ingest/simulate", json={"fixture_id": CLEAN, "via": "pigeon"}).status_code == 422
    assert client.post(f"{API}/ingest/simulate", json={"fixture_id": CLEAN, "channel": "fax"}).status_code == 422
    assert client.post(f"{API}/ingest/simulate", json={}).status_code == 422


def test_repeated_simulation_of_one_fixture_creates_distinct_conversations(client: TestClient) -> None:
    # /ingest/simulate adds a fresh nonce to the source interaction id, so it is never de-duplicated.
    first, second = start_ingest(client, CLEAN), start_ingest(client, CLEAN)
    assert first != second
    interactions = [wait_for_status(client, c)["record"]["meta"]["source_interaction_id"] for c in (first, second)]
    assert interactions[0] != interactions[1]
    assert len(os.path.commonprefix(interactions)) >= 8


def test_simulation_overrides_channel_business_line_and_template(client: TestClient) -> None:
    _, zoom = ingest_fixture(client, CLEAN, channel="zoom")
    assert zoom["record"]["meta"]["source"] == "zoom"
    assert zoom["record"]["transcript_origin"] == "zoom_platform"

    templates = {t["template_id"]: t for t in client.get(f"{API}/templates").json()}
    retail = next(t["template_id"] for t in templates.values() if "retail" in t["business_lines"])
    _, by_line = ingest_fixture(client, CLEAN, business_line="retail")
    assert (by_line["record"]["meta"]["business_line"], by_line["record"]["template_id"]) == ("retail", retail)
    _, by_template = ingest_fixture(client, CLEAN, template_id=retail)
    assert by_template["record"]["proposal"]["template_id"] == retail


def test_audio_path_goes_through_the_mock_transcriber(client: TestClient) -> None:
    _, detail = ingest_fixture(client, CLEAN, via="audio")
    assert detail["record"]["transcript_origin"] == "fixture"
    transcribe = [u for u in detail["usage"] if u["stage"] == "transcribe"]
    assert [(u["service"], u["provider"]) for u in transcribe] == [("speech", "mock")]
    assert _proposal(detail)["result"]["products"]


def test_asr_noise_option_still_produces_a_proposal(client: TestClient) -> None:
    _, detail = ingest_fixture(client, CLEAN, asr_noise=True)
    assert _proposal(detail)["result"]["tasks"]


@pytest.mark.parametrize("fixture_id", [
    next(fid for fid, call in CALLS.items() if call.channel.value == channel)
    for channel in ("teams", "zoom", "genesys", "mobile")
] + [next(fid for fid, call in CALLS.items() if call.gold.expected_language == "es"),
     next(fid for fid, call in CALLS.items() if call.market == "EU")])
def test_every_channel_language_and_market_reaches_review_with_gold_metadata(client: TestClient,
                                                                              fixture_id: str) -> None:
    call = CALLS[fixture_id]
    _, detail = ingest_fixture(client, fixture_id)
    record, proposal = detail["record"], _proposal(detail)
    assert record["meta"]["source"] == call.channel.value
    assert record["meta"]["market"] == call.market
    assert record["language"].startswith(call.gold.expected_language)
    assert record["template_id"] == proposal["template_id"] == call.gold.template_id
    assert proposal["crm_match"]["account"]["id"] == call.gold.expect_crm_account
    assert proposal["result"]["products"] and proposal["result"]["topics"]


def test_genesys_inbound_call_matches_the_crm_contact_by_phone_number(client: TestClient) -> None:
    call = CALLS[GENESYS_PHONE]
    _, detail = ingest_fixture(client, GENESYS_PHONE)
    match = _proposal(detail)["crm_match"]
    assert match["method"] == "phone"
    assert match["account"]["id"] == call.gold.expect_crm_account
    assert match["contact"] is not None
    assert detail["record"]["transcript_origin"] == "genesys_platform"
    assert detail["record"]["meta"]["direction"] == "inbound"


# ---- listing + detail ----------------------------------------------------------------------------------------------
def test_list_shows_the_pending_conversation_with_review_summary(client: TestClient,
                                                                 pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, _ = pending_clean
    call, gold = CALLS[CLEAN], CALLS[CLEAN].gold
    items = client.get(f"{API}/conversations", params={"limit": 500}).json()
    item = next(i for i in items if i["conversation_id"] == conversation_id)
    assert item["status"] == "needs_review" and item["needs_review"] is True
    assert (item["source"], item["market"], item["fixture_id"]) == (call.channel.value, call.market, CLEAN)
    assert item["product_count"] == len(gold.products)
    assert item["task_count"] == len(gold.tasks)
    assert item["topic_count"] >= len(gold.topics)
    assert item["template_id"] == gold.template_id
    assert item["seller_name"] == call.seller.display_name
    assert item["customer_org"] == call.customer_org
    assert item["review_reasons"] == [] and item["auto_approve_eligible"] is True
    assert item["latency_seconds"] > 0
    assert item["cost_usd"] >= 0


def test_list_puts_conversations_awaiting_review_first(client: TestClient,
                                                       pending_clean: tuple[str, dict[str, Any]],
                                                       written_us: tuple[str, dict[str, Any]]) -> None:
    items = client.get(f"{API}/conversations", params={"limit": 500}).json()
    statuses = [i["status"] for i in items]
    assert "needs_review" in statuses and "written" in statuses
    last_review = max(n for n, s in enumerate(statuses) if s == "needs_review")
    first_other = min(n for n, s in enumerate(statuses) if s != "needs_review")
    assert last_review < first_other
    created = [i["created_at"] for i in items if i["status"] == "needs_review"]
    assert created == sorted(created, reverse=True)


def test_list_filters_by_status_and_paginates(client: TestClient, pending_clean: tuple[str, dict[str, Any]],
                                              written_us: tuple[str, dict[str, Any]]) -> None:
    pending_id, written_id = pending_clean[0], written_us[0]
    review = client.get(f"{API}/conversations", params={"status": "needs_review", "limit": 500}).json()
    assert {i["status"] for i in review} == {"needs_review"}
    assert pending_id in {i["conversation_id"] for i in review} and written_id not in {i["conversation_id"] for i in review}

    both = client.get(f"{API}/conversations", params={"status": ["written", "rejected"], "limit": 500}).json()
    assert {i["status"] for i in both} <= {"written", "rejected"}
    assert written_id in {i["conversation_id"] for i in both}

    assert len(client.get(f"{API}/conversations", params={"limit": 1}).json()) == 1
    assert client.get(f"{API}/conversations", params={"offset": 10_000}).json() == []
    assert client.get(f"{API}/conversations", params={"status": "bogus"}).status_code == 422
    assert client.get(f"{API}/conversations", params={"limit": 501}).status_code == 422


def test_detail_has_proposal_stage_timeline_usage_and_cost(client: TestClient,
                                                           pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = pending_clean
    assert set(detail) == {"record", "stages", "events", "usage", "cost", "transcript_available", "transcript_note",
                           "vault_items"}
    stages = detail["stages"]
    names = [s["stage"] for s in stages]
    assert names == [s for s in STAGE_ORDER if s in names], "stages must follow the pipeline order"
    assert {"ingest", "transcribe", "normalize", "policy", "extract", "resolve", "validate", "review"} <= set(names)
    for stage in stages:
        if stage["stage"] != "review":
            assert stage["status"] in ("completed", "skipped")
            assert isinstance(stage["duration_ms"], int) and stage["duration_ms"] >= 0
            assert stage["started_at"]
    assert next(s for s in stages if s["stage"] == "review")["status"] == "started"
    assert next(s for s in stages if s["stage"] == "extract")["detail"]["engine"] == "rules"
    assert detail["events"][0]["stage"] == "ingest" and detail["events"][0]["conversation_id"] == conversation_id

    assert {u["stage"] for u in detail["usage"]} >= {"policy", "extract"}
    assert all(u["service"] and u["conversation_id"] == conversation_id for u in detail["usage"])
    cost = detail["cost"]
    assert cost["lines"] and cost["currency"] == "USD" and cost["illustrative"] is True
    assert cost["total"] >= 0
    listed = next(i for i in client.get(f"{API}/conversations", params={"limit": 500}).json()
                  if i["conversation_id"] == conversation_id)
    assert listed["cost_usd"] == pytest.approx(cost["total"])


def test_proposal_is_consistent_with_its_crm_preview(pending_clean: tuple[str, dict[str, Any]]) -> None:
    proposal = _proposal(pending_clean[1])
    preview = proposal["crm_preview"]
    assert proposal["note_markdown"].startswith("#")
    assert proposal["validation"]["passed"] is True and proposal["validation"]["leak_count"] == 0
    assert preview["phone_call"]["description"] == proposal["note_markdown"]
    assert preview["conversation_id"] == pending_clean[0]
    assert len(preview["tasks"]) == len(proposal["result"]["tasks"])
    assert preview["insight"]["transcript_pointer"] is None
    assert preview["idempotency_key"].endswith(":v1")


def test_proposal_matches_the_gold_labels_of_the_scripted_call(pending_clean: tuple[str, dict[str, Any]]) -> None:
    gold = CALLS[CLEAN].gold
    proposal = _proposal(pending_clean[1])
    result = proposal["result"]
    assert {m["chosen_product_id"] for m in result["products"]} == {g.product_id for g in gold.products}
    assert all(m["needs_review"] is False for m in result["products"])
    assert {t["topic_id"] for t in result["topics"]} >= set(gold.topics)
    for expected in gold.tasks:
        text = [(t["title"] + " " + (t["description"] or "")).lower() for t in result["tasks"] if
                t["due_date"] == expected.due_date.isoformat()]
        assert any(all(k.lower() in blob for k in expected.keywords) for blob in text), expected.keywords
    assert proposal["crm_match"]["account"]["id"] == gold.expect_crm_account
    assert proposal["review"]["reasons"] == []
    assert proposal["template_id"] == gold.template_id


# ---- transcript + vault while pending -------------------------------------------------------------------------------
def test_transcript_is_the_redacted_working_copy_while_review_is_pending(
        client: TestClient, pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = pending_clean
    assert detail["transcript_available"] is True
    assert "working copy" in detail["transcript_note"].lower()
    response = client.get(f"{API}/conversations/{conversation_id}/transcript")
    assert response.status_code == 200
    assert response.headers["X-Transcript-Source"] == "working"
    conversation = response.json()
    assert conversation["meta"]["conversation_id"] == conversation_id
    assert conversation["segments"] and all(s["text"] for s in conversation["segments"])
    spoken = norm(" ".join(s["text"] for s in conversation["segments"]))
    assert any(norm(turn) in spoken for turn in longest_turns(CALLS[CLEAN]))  # positive control for the leak checks


def test_pending_conversation_has_only_a_short_lived_redacted_working_copy_in_the_vault(
        client: TestClient, pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = pending_clean
    items = client.get(f"{API}/vault/items", params={"conversation_id": conversation_id}).json()
    assert [(i["kind"], i["redacted"]) for i in items] == [("working", True)]
    assert [i["kind"] for i in detail["vault_items"]] == ["working"]
    assert items[0]["expires_at"] is not None
    assert _parse(items[0]["expires_at"]) <= datetime.now(UTC) + timedelta(hours=25)
    assert conversation_id in {i["conversation_id"] for i in client.get(f"{API}/vault/items").json()}


def test_retention_sweep_keeps_unexpired_items(client: TestClient, pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, _ = pending_clean
    swept = client.post(f"{API}/retention/sweep")
    assert swept.status_code == 200
    assert conversation_id not in {i["conversation_id"] for i in swept.json()["deleted"]}
    assert client.get(f"{API}/conversations/{conversation_id}/transcript").status_code == 200


def test_unknown_conversation_is_404_everywhere(client: TestClient) -> None:
    for method, path, kwargs in (("get", "", {}), ("get", "/transcript", {}), ("post", "/approve", {}),
                                 ("post", "/reject", {}), ("post", "/reprocess", {"json": {}})):
        response = getattr(client, method)(f"{API}/conversations/does-not-exist{path}", **kwargs)
        assert response.status_code == 404, path
        assert "not found" in response.json()["detail"]


# ---- approval (one click) ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [None, {}], ids=["no-body", "empty-json"])
def test_one_click_approve_writes_call_tasks_and_insight_to_the_crm(client: TestClient, body: dict[str, Any] | None) -> None:
    gold = CALLS[CLEAN].gold
    conversation_id, pending = ingest_fixture(client, CLEAN)
    before = {e: len(crm_rows(client, e)) for e in CRM_ENTITIES}
    detail = approve(client, conversation_id, body)
    record = detail["record"]

    assert record["status"] == "written"
    assert record["crm_result"]["ok"] is True and record["crm_result"]["skipped_duplicate"] is False
    created = [(r["entity"], r["id"]) for r in record["crm_result"]["created"]]
    assert [e for e, _ in created].count("phonecall") == 1
    assert [e for e, _ in created].count("insight") == 1
    assert [e for e, _ in created].count("task") == len(_proposal(pending)["result"]["tasks"])
    review = _proposal(detail)["review"]
    assert (review["state"], review["clicks"], review["reviewed_by"]) == ("approved", 1, "reviewer")
    assert review["seconds_to_review"] is not None and review["seconds_to_review"] >= 0
    assert {"review", "retention", "write"} <= {s["stage"] for s in detail["stages"] if s["status"] == "completed"}

    bundle = _crm_bundle(client, conversation_id)
    assert {e: len(rows) for e, rows in bundle.items()} == {"phonecall": 1, "task": len(gold.tasks), "insight": 1}
    assert {e: len(crm_rows(client, e)) for e in CRM_ENTITIES} == {
        "phonecall": before["phonecall"] + 1, "task": before["task"] + len(gold.tasks), "insight": before["insight"] + 1}
    assert {id_ for e, id_ in created if e == "phonecall"} == {bundle["phonecall"][0]["id"]}
    assert {id_ for e, id_ in created if e == "task"} == {t["id"] for t in bundle["task"]}

    phone_call, insight = bundle["phonecall"][0], bundle["insight"][0]
    assert phone_call["description"] == _proposal(detail)["note_markdown"]
    assert phone_call["owner_email"] == CALLS[CLEAN].seller.email
    assert phone_call["regarding_id"] and phone_call["contact_name"]
    assert {t["due"] for t in bundle["task"]} == {g.due_date.isoformat() for g in gold.tasks}
    assert all(t["phonecall_id"] == phone_call["id"] and t["owner_email"] == phone_call["owner_email"]
               for t in bundle["task"])
    assert insight["phonecall_id"] == phone_call["id"]
    assert insight["product_ids"] == [g.product_id for g in gold.products]
    assert set(insight["topic_ids"]) >= set(gold.topics)
    assert (insight["template_id"], insight["review_status"], insight["engine"]) == (gold.template_id, "approved", "rules")


def test_approving_twice_is_a_conflict_and_does_not_duplicate_crm_rows(
        client: TestClient, written_us: tuple[str, dict[str, Any]]) -> None:
    conversation_id, _ = written_us
    before = {e: len(crm_rows(client, e, conversation_id)) for e in CRM_ENTITIES}
    for path, body in (("approve", None), ("reject", None), ("reprocess", {"template_id": None})):
        response = client.post(f"{API}/conversations/{conversation_id}/{path}", **({} if body is None else {"json": body}))
        assert response.status_code == 409, path
        assert "written" in response.json()["detail"]
    assert {e: len(crm_rows(client, e, conversation_id)) for e in CRM_ENTITIES} == before


# ---- the CRM never receives transcript text --------------------------------------------------------------------------
def test_crm_records_never_contain_transcript_text(client: TestClient, written_us: tuple[str, dict[str, Any]],
                                                   pending_clean: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = written_us
    _assert_no_transcript_text(CLEAN, _crm_bundle(client, conversation_id))
    _assert_no_transcript_text(CLEAN, _proposal(detail)["crm_preview"])
    assert _crm_bundle(client, conversation_id)["insight"][0]["transcript_pointer"] is None
    # positive control: the same check does flag the redacted working copy, which does hold the turns
    working = client.get(f"{API}/conversations/{pending_clean[0]}/transcript").json()
    with pytest.raises(AssertionError):
        _assert_no_transcript_text(CLEAN, working)


def test_us_market_keeps_no_transcript_after_approval(client: TestClient, written_us: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = written_us
    retention = detail["record"]["retention"]
    assert (retention["market"], retention["transcript_stored"], retention["crm_contains_transcript"]) == ("US", False, False)
    assert detail["vault_items"] == []
    assert client.get(f"{API}/vault/items", params={"conversation_id": conversation_id}).json() == []
    assert detail["transcript_available"] is False

    response = client.get(f"{API}/conversations/{conversation_id}/transcript")
    assert response.status_code == 404
    assert response.json()["detail"] == retention["explanation"] == detail["transcript_note"]
    result = _proposal(detail)["result"]
    quotes = [ev["quote"] for group in ("products", "topics", "tasks", "relationship_facts") for item in result[group]
              for ev in item["evidence"]]
    assert quotes and all(q == "" for q in quotes), "verbatim evidence quotes must be dropped when no transcript is kept"


@pytest.mark.parametrize("market", ["UK", "EU"])
def test_uk_and_eu_markets_keep_a_redacted_transcript_in_the_vault(client: TestClient, market: str) -> None:
    policy = next(m for m in client.get(f"{API}/policies").json()["markets"] if m["market"] == market)
    conversation_id, pending = ingest_fixture(client, CLEAN, market=market)
    assert pending["record"]["meta"]["market"] == market
    assert _proposal(pending)["crm_preview"]["insight"]["transcript_pointer"].startswith("vault://")

    detail = approve(client, conversation_id)
    retention = detail["record"]["retention"]
    assert detail["record"]["status"] == "written"
    assert retention["transcript_stored"] is True and retention["crm_contains_transcript"] is False
    expected_expiry = datetime.now(UTC) + timedelta(days=policy["transcript_retention_days"])
    assert abs(_parse(retention["transcript_expires_at"]) - expected_expiry) < timedelta(days=1)

    items = client.get(f"{API}/vault/items", params={"conversation_id": conversation_id}).json()
    assert [(i["kind"], i["redacted"]) for i in items] == [("transcript", True)]
    assert _parse(items[0]["expires_at"]) == _parse(retention["transcript_expires_at"])
    assert [i["kind"] for i in detail["vault_items"]] == ["transcript"]
    assert detail["transcript_available"] is True

    response = client.get(f"{API}/conversations/{conversation_id}/transcript")
    assert response.status_code == 200 and response.headers["X-Transcript-Source"] == "vault"
    assert response.json()["segments"]

    bundle = _crm_bundle(client, conversation_id)
    assert bundle["insight"][0]["transcript_pointer"] == items[0]["uri"]
    assert URI.match(items[0]["uri"])
    _assert_no_transcript_text(CLEAN, bundle)


# ---- ambiguous product: reviewer pick --------------------------------------------------------------------------------
def test_ambiguous_fund_is_flagged_for_a_product_pick_and_never_auto_approvable(
        client: TestClient, pending_ambiguous: tuple[str, dict[str, Any]]) -> None:
    conversation_id, detail = pending_ambiguous
    gold = CALLS[AMBIGUOUS].gold.products[0]
    proposal = _proposal(detail)
    assert "product_needs_pick" in proposal["review"]["reasons"]
    assert proposal["review"]["auto_approve_eligible"] is False

    mention = proposal["result"]["products"][0]
    assert mention["chosen_product_id"] is None and mention["needs_review"] is True
    assert "share_class_not_stated" in mention["reasons"]
    candidates = [c["product_id"] for c in mention["candidates"]]
    assert 2 <= len(candidates) <= 3
    assert set(candidates) & set(gold.acceptable_ids)

    item = next(i for i in client.get(f"{API}/conversations", params={"limit": 500}).json()
                if i["conversation_id"] == conversation_id)
    assert "product_needs_pick" in item["review_reasons"] and item["auto_approve_eligible"] is False


def test_approve_does_not_force_a_product_pick_and_writes_the_mention_as_unconfirmed(client: TestClient) -> None:
    # Real behaviour: the review reason is advisory for manual approval (only auto-approve is blocked by it).
    conversation_id, _ = ingest_fixture(client, AMBIGUOUS)
    detail = approve(client, conversation_id, {})
    assert detail["record"]["status"] == "written"
    assert _mentions(detail)[0]["chosen_product_id"] is None
    bundle = _crm_bundle(client, conversation_id)
    assert "to be confirmed" in bundle["phonecall"][0]["description"].lower()
    assert bundle["insight"][0]["product_ids"] == []


def test_share_class_pick_resolves_the_mention_without_learning_an_alias(client: TestClient) -> None:
    gold = CALLS[AMBIGUOUS].gold.products[0]
    assert client.get(f"{API}/aliases").json() == []
    conversation_id, pending = ingest_fixture(client, AMBIGUOUS)
    mention = _mentions(pending)[0]
    pick = next(c["product_id"] for c in mention["candidates"] if c["product_id"] in gold.acceptable_ids)

    detail = approve(client, conversation_id, {"product_choices": {mention["mention_id"]: pick}})
    chosen = _mentions(detail)[0]
    assert detail["record"]["status"] == "written"
    assert chosen["chosen_product_id"] == pick and chosen["needs_review"] is False and chosen["confidence"] == 1.0
    assert "reviewer_selected" in chosen["reasons"]
    assert _crm_bundle(client, conversation_id)["insight"][0]["product_ids"] == [pick]
    assert client.get(f"{API}/aliases").json() == []
    assert client.get(f"{API}/catalogue/stats").json()["learned_aliases"] == 0


def test_correcting_a_product_whose_class_was_stated_learns_an_alias(client: TestClient) -> None:
    conversation_id, pending = ingest_fixture(client, CLEAN)
    mention = _mentions(pending)[0]
    assert "share_class_not_stated" not in mention["reasons"]
    other = next(c["product_id"] for c in mention["candidates"] if c["product_id"] != mention["chosen_product_id"])
    try:
        approve(client, conversation_id, {"product_choices": {mention["mention_id"]: other}})
        aliases = client.get(f"{API}/aliases").json()
        assert [(a["surface"], a["product_id"]) for a in aliases] == [(_surface_key(mention["surface"]), other)]
        assert client.get(f"{API}/catalogue/stats").json()["learned_aliases"] == 1
    finally:
        client.delete(f"{API}/aliases")
    assert client.get(f"{API}/aliases").json() == []


def test_invalid_product_choices_are_rejected_and_leave_the_conversation_pending(client: TestClient) -> None:
    conversation_id, pending = ingest_fixture(client, AMBIGUOUS)
    mention_id = _mentions(pending)[0]["mention_id"]
    candidate = _mentions(pending)[0]["candidates"][0]["product_id"]
    for choices in ({mention_id: "NOT-A-PRODUCT"}, {"no-such-mention": candidate}):
        response = client.post(f"{API}/conversations/{conversation_id}/approve", json={"product_choices": choices})
        assert response.status_code == 422
        assert "cannot apply product choice" in response.json()["detail"]
    assert client.get(f"{API}/conversations/{conversation_id}").json()["record"]["status"] == "needs_review"
    assert crm_rows(client, "phonecall", conversation_id) == []


# ---- reviewer edits ------------------------------------------------------------------------------------------------------
def test_reviewer_edits_flow_into_the_crm_records(client: TestClient) -> None:
    conversation_id, pending = ingest_fixture(client, CLEAN)
    tasks = _proposal(pending)["result"]["tasks"]
    chosen_topics = [t["topic_id"] for t in _proposal(pending)["result"]["topics"]][:2]
    assert len(chosen_topics) == 2
    edit = "Edited overview by the reviewer."
    detail = approve(client, conversation_id, {
        "task_edits": [{"task_id": tasks[1]["task_id"], "drop": True},
                       {"task_id": tasks[0]["task_id"], "title": "Send the fee table", "due_date": "2026-11-14"}],
        "topic_ids": chosen_topics, "section_edits": {"overview": edit}, "clicks": 3})
    assert detail["record"]["status"] == "written"
    assert _proposal(detail)["review"]["clicks"] == 3
    assert [t["topic_id"] for t in _proposal(detail)["result"]["topics"]] == chosen_topics

    bundle = _crm_bundle(client, conversation_id)
    assert [(t["subject"], t["due"]) for t in bundle["task"]] == [("Send the fee table", "2026-11-14")]
    assert edit in bundle["phonecall"][0]["description"]
    assert bundle["insight"][0]["topic_ids"] == chosen_topics
    overview = next(s for s in bundle["insight"][0]["sections"] if s["key"] == "overview")
    assert overview["text"] == edit


def test_dropping_a_product_mention_removes_it_from_the_crm_insight(client: TestClient) -> None:
    conversation_id, pending = ingest_fixture(client, CLEAN)
    mention_id = _mentions(pending)[0]["mention_id"]
    detail = approve(client, conversation_id, {"drop_mentions": [mention_id]})
    assert _mentions(detail) == []
    assert _crm_bundle(client, conversation_id)["insight"][0]["product_ids"] == []


def test_edit_that_introduces_sensitive_content_is_refused(client: TestClient, fresh_clean: str) -> None:
    sensitive = {"section_edits": {"overview": "Customer paid with card 4111 1111 1111 1111 after the cancer diagnosis."}}
    response = client.post(f"{API}/conversations/{fresh_clean}/approve", json=sensitive)
    assert response.status_code == 422
    assert "sensitive" in response.json()["detail"]
    assert client.get(f"{API}/conversations/{fresh_clean}").json()["record"]["status"] == "needs_review"
    assert crm_rows(client, "phonecall", fresh_clean) == []


def test_approve_with_template_id_rerenders_with_that_template_before_writing(client: TestClient,
                                                                              fresh_clean: str) -> None:
    template = next(t for t in client.get(f"{API}/templates").json() if t["template_id"] != CALLS[CLEAN].gold.template_id)
    detail = approve(client, fresh_clean, {"template_id": template["template_id"]})
    assert detail["record"]["status"] == "written"
    assert _proposal(detail)["template_id"] == template["template_id"]
    insight = _crm_bundle(client, fresh_clean)["insight"][0]
    assert insight["template_id"] == template["template_id"]
    assert [s["key"] for s in insight["sections"]] == [s["key"] for s in template["sections"]]


# ---- reject ----------------------------------------------------------------------------------------------------------
def test_reject_marks_the_conversation_rejected_and_writes_nothing_to_the_crm(client: TestClient) -> None:
    conversation_id, _ = ingest_fixture(client, CLEAN)
    before = {e: len(crm_rows(client, e)) for e in CRM_ENTITIES}
    response = client.post(f"{API}/conversations/{conversation_id}/reject", json={"reason": "wrong customer"})
    assert response.status_code == 200
    detail = response.json()
    assert detail["record"]["status"] == "rejected"
    assert _proposal(detail)["review"]["state"] == "rejected"
    assert detail["record"]["crm_result"] is None
    assert detail["vault_items"] == [] and detail["transcript_available"] is False

    assert {e: len(crm_rows(client, e)) for e in CRM_ENTITIES} == before
    assert _crm_bundle(client, conversation_id) == {e: [] for e in CRM_ENTITIES}
    assert client.get(f"{API}/vault/items", params={"conversation_id": conversation_id}).json() == []
    assert client.get(f"{API}/conversations/{conversation_id}/transcript").status_code == 404
    rejected = client.get(f"{API}/conversations", params={"status": "rejected", "limit": 500}).json()
    assert conversation_id in {i["conversation_id"] for i in rejected}

    for path, body in (("approve", None), ("reject", None), ("reprocess", {})):
        again = client.post(f"{API}/conversations/{conversation_id}/{path}", **({} if body is None else {"json": body}))
        assert again.status_code == 409, path
        assert "rejected" in again.json()["detail"]


def test_reject_without_a_body_is_accepted(client: TestClient, fresh_clean: str) -> None:
    response = client.post(f"{API}/conversations/{fresh_clean}/reject")
    assert response.status_code == 200 and response.json()["record"]["status"] == "rejected"


# ---- reprocess -------------------------------------------------------------------------------------------------------
def test_reprocess_with_another_template_changes_the_proposal_template(client: TestClient) -> None:
    conversation_id, before = ingest_fixture(client, CLEAN)
    templates = {t["template_id"]: t for t in client.get(f"{API}/templates").json()}
    other = next(t for t in templates.values() if t["template_id"] != _proposal(before)["template_id"])

    response = client.post(f"{API}/conversations/{conversation_id}/reprocess", json={"template_id": other["template_id"]})
    assert response.status_code == 200
    detail = response.json()
    assert detail["record"]["status"] == "needs_review"
    assert detail["record"]["template_id"] == _proposal(detail)["template_id"] == other["template_id"]
    assert _proposal(detail)["template_id"] != _proposal(before)["template_id"]
    assert [s["key"] for s in _proposal(detail)["result"]["sections"]] == [s["key"] for s in other["sections"]]
    assert _proposal(detail)["review"]["state"] == "pending"
    assert detail["transcript_available"] is True
    assert {t["title"] for t in _proposal(detail)["result"]["tasks"]} == {t["title"] for t in _proposal(before)["result"]["tasks"]}

    written = approve(client, conversation_id)
    assert _crm_bundle(client, conversation_id)["insight"][0]["template_id"] == other["template_id"]
    assert written["record"]["status"] == "written"


def test_reprocess_with_an_unavailable_engine_falls_back_to_rules_and_says_so(client: TestClient,
                                                                              fresh_clean: str) -> None:
    response = client.post(f"{API}/conversations/{fresh_clean}/reprocess", json={"engine": "llm"})
    assert response.status_code == 200
    detail = response.json()
    assert detail["record"]["status"] == "needs_review" and detail["record"]["engine"] == "rules"
    assert "engine_unavailable:llm" in _proposal(detail)["result"]["flags"]


def test_reprocess_with_an_unknown_template_is_rejected_and_keeps_the_conversation_pending(
        client: TestClient, fresh_clean: str) -> None:
    before = client.get(f"{API}/conversations/{fresh_clean}").json()
    response = client.post(f"{API}/conversations/{fresh_clean}/reprocess", json={"template_id": "no-such-template"})
    assert response.status_code == 422
    assert "unknown template" in response.json()["detail"]

    after = client.get(f"{API}/conversations/{fresh_clean}").json()
    assert after["record"]["status"] == "needs_review" and after["record"]["error"] is None
    assert after["record"]["template_id"] == _proposal(after)["template_id"] == _proposal(before)["template_id"]
    assert after["transcript_available"] is True
    assert approve(client, fresh_clean)["record"]["status"] == "written"


def test_approve_with_an_unknown_template_is_rejected_and_writes_nothing(client: TestClient, fresh_clean: str) -> None:
    response = client.post(f"{API}/conversations/{fresh_clean}/approve", json={"template_id": "no-such-template"})
    assert response.status_code == 422
    assert "unknown template" in response.json()["detail"]

    record = client.get(f"{API}/conversations/{fresh_clean}").json()["record"]
    assert (record["status"], record["error"], record["crm_result"]) == ("needs_review", None, None)
    assert record["proposal"]["review"]["state"] == "pending"
    assert _crm_bundle(client, fresh_clean) == {e: [] for e in CRM_ENTITIES}
    assert approve(client, fresh_clean)["record"]["status"] == "written"


def test_reviewer_topic_selection_reaches_the_crm_insight(client: TestClient, fresh_clean: str) -> None:
    detail = approve(client, fresh_clean, {"topic_ids": ["fees_expenses"]})
    assert detail["record"]["status"] == "written"
    assert [t["topic_id"] for t in _proposal(detail)["result"]["topics"]] == ["fees_expenses"]
    assert _proposal(detail)["validation"]["unsupported_claims"] == 0
    assert _crm_bundle(client, fresh_clean)["insight"][0]["topic_ids"] == ["fees_expenses"]


def test_reviewer_can_add_a_topic_the_engine_did_not_detect_and_unknown_ids_are_ignored(client: TestClient) -> None:
    # UK keeps the evidence quotes after approval, so the reviewer-asserted evidence marker stays visible.
    conversation_id, pending = ingest_fixture(client, CLEAN, market="UK")
    detected = {t["topic_id"]: t for t in _proposal(pending)["result"]["topics"]}
    kept = next(iter(detected))
    added = next(t["topic_id"] for t in client.get(f"{API}/taxonomy").json()["topics"] if t["topic_id"] not in detected)

    detail = approve(client, conversation_id, {"topic_ids": [added, kept, "no-such-topic"]})
    assert detail["record"]["status"] == "written"
    topics = {t["topic_id"]: t for t in _proposal(detail)["result"]["topics"]}
    assert list(topics) == [added, kept]
    assert topics[added]["evidence"] == [{"segment_ids": [], "quote": REVIEWER_EVIDENCE}]
    assert [e["segment_ids"] for e in topics[kept]["evidence"]] == [e["segment_ids"] for e in detected[kept]["evidence"]]
    assert all(e["segment_ids"] for e in topics[kept]["evidence"])
    assert _proposal(detail)["validation"]["unsupported_claims"] == 0
    assert _crm_bundle(client, conversation_id)["insight"][0]["topic_ids"] == [added, kept]


# ---- privacy -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fixture_id", PRIVACY_FIXTURES)
def test_sensitive_strings_never_reach_the_proposal_or_the_crm(client: TestClient, fixture_id: str) -> None:
    call = CALLS[fixture_id]
    conversation_id, pending = ingest_fixture(client, fixture_id)
    proposal_json = json.dumps(_proposal(pending), ensure_ascii=False)
    assert not [s for s in call.gold.must_not_leak if contains_leak(proposal_json, s)]
    assert _proposal(pending)["validation"]["leak_count"] == 0

    approve(client, conversation_id)
    crm_json = json.dumps(_crm_bundle(client, conversation_id), ensure_ascii=False)
    assert not [s for s in call.gold.must_not_leak if contains_leak(crm_json, s)]


@pytest.mark.parametrize("fixture_id", PRIVACY_FIXTURES)
def test_redacted_working_copy_hides_the_sensitive_strings(client: TestClient, fixture_id: str) -> None:
    conversation_id, _ = ingest_fixture(client, fixture_id)
    response = client.get(f"{API}/conversations/{conversation_id}/transcript")
    assert response.headers["X-Transcript-Source"] == "working"
    assert not [s for s in CALLS[fixture_id].gold.must_not_leak if contains_leak(response.text, s)]


@pytest.mark.parametrize("fixture_id", PRIVACY_FIXTURES)
def test_transcript_retained_in_the_vault_hides_the_sensitive_strings(client: TestClient, fixture_id: str) -> None:
    conversation_id, _ = ingest_fixture(client, fixture_id, market="UK")
    approve(client, conversation_id)
    response = client.get(f"{API}/conversations/{conversation_id}/transcript")
    assert response.status_code == 200 and response.headers["X-Transcript-Source"] == "vault"
    assert not [s for s in CALLS[fixture_id].gold.must_not_leak if contains_leak(response.text, s)]


def test_relationship_context_is_kept_while_sensitive_details_are_omitted(client: TestClient) -> None:
    fixture_id = "retail-teams-relationship-privacy"
    gold = CALLS[fixture_id].gold
    conversation_id, pending = ingest_fixture(client, fixture_id)
    proposal = _proposal(pending)
    kinds = {a["kind"] for a in proposal["policy_actions"]}
    assert {"kept_relationship", "omitted_sensitive"} <= kinds
    kept = norm(proposal["note_markdown"] + " " + " ".join(f["text"] for f in proposal["result"]["relationship_facts"]))
    assert all(keyword in kept for keyword in gold.must_keep)

    approve(client, conversation_id)
    insight = _crm_bundle(client, conversation_id)["insight"][0]
    assert insight["relationship_facts"]
    assert all(keyword in norm(" ".join(insight["relationship_facts"]) + insight["summary_markdown"])
               for keyword in gold.must_keep)

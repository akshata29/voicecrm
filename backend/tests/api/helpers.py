"""Shared helpers for the HTTP API tests: ingest + polling, CRM row access and transcript-text normalisation."""
from __future__ import annotations

import re
import time
from collections.abc import Iterable, Iterator
from functools import lru_cache
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecrm.data_loader import load_scripted_calls
from voicecrm.domain.fixtures import ScriptedCall

API = "/api/v1"
SETTLED = ("needs_review", "failed")
_SPACE = re.compile(r"\s+")


@lru_cache(maxsize=1)
def scripted_calls() -> dict[str, ScriptedCall]:
    return {call.fixture_id: call for call in load_scripted_calls()}


def wait_for_status(client: TestClient, conversation_id: str, statuses: Iterable[str] = SETTLED,
                    timeout: float = 90.0) -> dict[str, Any]:
    """Polls GET /conversations/{id} until the status is one of `statuses`; returns the ConversationDetail JSON."""
    wanted = tuple(statuses)
    deadline = time.monotonic() + timeout
    status = "unknown"
    while time.monotonic() < deadline:
        response = client.get(f"{API}/conversations/{conversation_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        status = detail["record"]["status"]
        if status in wanted:
            return detail
        time.sleep(0.05)
    pytest.fail(f"conversation {conversation_id} did not reach {wanted} within {timeout}s (last status: {status})")


def start_ingest(client: TestClient, fixture_id: str, **overrides: Any) -> str:
    """POST /ingest/simulate for a scripted call; returns the conversation id without waiting."""
    response = client.post(f"{API}/ingest/simulate", json={"fixture_id": fixture_id, **overrides})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duplicate_of"] is None
    return body["conversation_id"]


def ingest_fixture(client: TestClient, fixture_id: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    """Ingests a scripted call and waits until it is ready for review. Returns (conversation_id, detail)."""
    conversation_id = start_ingest(client, fixture_id, **overrides)
    detail = wait_for_status(client, conversation_id)
    record = detail["record"]
    assert record["status"] == "needs_review", f"{fixture_id} ended as {record['status']}: {record.get('error')}"
    return conversation_id, detail


def approve(client: TestClient, conversation_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approves (empty body unless given) and returns the ConversationDetail JSON."""
    response = client.post(f"{API}/conversations/{conversation_id}/approve", **({} if body is None else {"json": body}))
    assert response.status_code == 200, response.text
    return response.json()


def crm_rows(client: TestClient, entity: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
    response = client.get(f"{API}/crm/records", params={"entity": entity, "limit": 500})
    assert response.status_code == 200, response.text
    rows = response.json()
    return [row for row in rows if conversation_id is None or row.get("conversation_id") == conversation_id]


def norm(text: str) -> str:
    """Case-, whitespace- and quote-style-insensitive form used for 'does this text appear in that text' checks."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return _SPACE.sub(" ", text).strip().casefold()


def iter_strings(value: Any) -> Iterator[str]:
    """Every string anywhere inside a JSON-like structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


def longest_turns(call: ScriptedCall, count: int = 3) -> list[str]:
    return sorted((turn.text for turn in call.turns), key=len, reverse=True)[:count]


def contains_leak(haystack: str, needle: str) -> bool:
    """Independent re-implementation of the leak check: normalised substring, or digits-only for long numbers."""
    if norm(needle) in norm(haystack):
        return True
    digits = re.sub(r"[\s\-]", "", needle)
    return sum(ch.isdigit() for ch in needle) >= 8 and digits in re.sub(r"[\s\-]", "", haystack)

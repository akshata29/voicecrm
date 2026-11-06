"""Packing transcript segments into Language-service documents: planning, billing, offsets, straddling entities."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from voicecrm.adapters.pii.azure_language import AzureLanguagePiiDetector
from voicecrm.adapters.pii.local import LocalPiiDetector
from voicecrm.adapters.pii.packing import (
    DEFAULT_PACK_CHARS,
    SEPARATOR,
    Document,
    distribute,
    plan_documents,
    split_long,
    text_records,
)
from voicecrm.ports.pii import PiiSpan

from .test_pii_azure_language import ENDPOINT, URL, Sleeps, entity, service

SEGMENTS = ["Hello, thanks for joining.", "Of course. My email is jane@contoso.com.", "", "   ", "Great, I'll send the fee comparison."]


def detector(**kw) -> AzureLanguagePiiDetector:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("key", "k")
    kw.setdefault("auth", "key")
    kw.setdefault("http_client", httpx.AsyncClient(verify=False))
    kw.setdefault("sleep", Sleeps())
    kw.setdefault("jitter", 0)
    kw.setdefault("merge_local", False)
    return AzureLanguagePiiDetector(**kw)


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_without_packing_every_non_blank_text_is_its_own_document():
    docs = plan_documents(SEGMENTS, pack_chars=0)
    assert [d.text for d in docs] == [SEGMENTS[0], SEGMENTS[1], SEGMENTS[4]]
    assert [(p.index, p.start, p.end, p.offset) for d in docs for p in d.parts] == [
        (0, 0, len(SEGMENTS[0]), 0), (1, 0, len(SEGMENTS[1]), 0), (4, 0, len(SEGMENTS[4]), 0)]


def test_packing_joins_consecutive_texts_and_remembers_where_each_sits():
    (doc,) = plan_documents(SEGMENTS, pack_chars=4000)
    assert doc.text == SEPARATOR.join([SEGMENTS[0], SEGMENTS[1], SEGMENTS[4]])
    for part, index in zip(doc.parts, (0, 1, 4), strict=True):
        assert part.index == index and doc.text[part.start:part.end] == SEGMENTS[index] and part.offset == 0


def test_a_document_is_closed_when_the_next_text_would_overflow_it():
    texts = ["a" * 60, "b" * 60, "c" * 60, "d" * 60]
    docs = plan_documents(texts, pack_chars=130)
    assert [[p.index for p in d.parts] for d in docs] == [[0, 1], [2, 3]]
    assert all(len(d.text) <= 130 for d in docs)


def test_texts_at_or_over_the_pack_size_get_their_own_document():
    texts = ["short one", "x" * 500, "short two"]
    docs = plan_documents(texts, pack_chars=300)
    assert [[p.index for p in d.parts] for d in docs] == [[0], [1], [2]]


def test_over_long_texts_are_split_and_keep_their_offsets():
    text = "palabra " * 1500                                      # 12,000 characters
    docs = plan_documents([text], max_chars=5120, pack_chars=4000)
    assert len(docs) >= 3 and all(len(d.text) <= 5120 for d in docs)
    assert "".join(d.text for d in docs) == text
    assert [p.offset for d in docs for p in d.parts] == [offset for offset, _ in split_long(text, 5120)]


def test_records_are_billed_per_thousand_characters_with_one_minimum_per_document():
    assert Document(text="x" * 1).records == 1 and Document(text="x" * 1000).records == 1
    assert Document(text="x" * 1001).records == 2 and Document(text="x" * 3999).records == 4
    texts = ["x" * 140] * 22                                      # a typical call: 22 segments of ~140 characters
    assert text_records(texts, pack_chars=0) == 22
    assert text_records(texts, pack_chars=DEFAULT_PACK_CHARS) == 4


def test_the_local_detector_projects_the_packed_bill():
    texts = ["x" * 140] * 22
    assert LocalPiiDetector().text_records(texts) == text_records(texts) == 4


def test_spans_are_mapped_back_to_their_own_text_and_clipped_where_they_straddle_a_boundary():
    first, second = "my card is 4111 1111", "1111 1111 and that is all"
    (doc,) = plan_documents([first, second], pack_chars=4000)
    number = "4111 1111\n1111 1111"
    start = doc.text.index(number)
    span = PiiSpan(start=start, end=start + len(number), category="payment_card", text=number, confidence=0.9,
                   source="azure_language")
    placed = distribute(doc, [span])
    assert [(index, s.text) for index, s in placed] == [(0, "4111 1111"), (1, "1111 1111")]
    assert first[placed[0][1].start:placed[0][1].end] == "4111 1111"
    assert second[placed[1][1].start:placed[1][1].end] == "1111 1111"


async def test_the_adapter_sends_one_packed_document_and_returns_per_text_spans(api):
    route = api.post(URL).mock(side_effect=service(
        lambda text: [entity(text, "jane@contoso.com", "Email")] if "jane@contoso.com" in text else []))
    out = await detector(pack_chars=4000).detect_many(SEGMENTS, language="en")
    assert route.call_count == 1
    documents = json.loads(route.calls.last.request.content)["analysisInput"]["documents"]
    assert len(documents) == 1 and documents[0]["text"] == SEPARATOR.join([SEGMENTS[0], SEGMENTS[1], SEGMENTS[4]])
    assert out[0] == [] and out[2] == [] and out[3] == [] and out[4] == []
    (span,) = out[1]
    assert (span.category, span.text, span.source) == ("personal_contact", "jane@contoso.com", "azure_language")
    assert SEGMENTS[1][span.start:span.end] == "jane@contoso.com"


async def test_a_number_spoken_across_two_turns_is_redacted_in_both(api):
    first, second = "the long number is 4111 1111", "1111 1111, got that?"
    api.post(URL).mock(side_effect=service(lambda text: [
        {"text": "4111 1111\n1111 1111", "category": "CreditCardNumber", "offset": text.index("4111 1111\n1111"),
         "length": len("4111 1111\n1111 1111"), "confidenceScore": 0.9}]))
    one, two = await detector(pack_chars=4000).detect_many([first, second], language="en")
    assert [first[s.start:s.end] for s in one] == ["4111 1111"] and [second[s.start:s.end] for s in two] == ["1111 1111"]
    assert one[0].category == two[0].category == "payment_card"


async def test_packing_reduces_requests_and_the_billed_records(api):
    route = api.post(URL).mock(side_effect=service(lambda text: []))
    texts = [f"Segment number {i} of a call that goes on for a while, with some ordinary business talk." for i in range(22)]
    flat, packed = detector(pack_chars=0), detector(pack_chars=4000)
    await flat.detect_many(texts, language="en")
    flat_requests = route.call_count
    await packed.detect_many(texts, language="en")
    assert (flat_requests, route.call_count - flat_requests) == (5, 1)
    assert (flat.text_records(texts), packed.text_records(texts)) == (22, 2)


async def test_the_pack_size_is_capped_at_the_service_document_limit():
    assert detector(pack_chars=99999)._pack_chars == 5120
    assert detector(pack_chars=-5)._pack_chars == 0

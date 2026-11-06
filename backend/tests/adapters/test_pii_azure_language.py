"""AzureLanguagePiiDetector: request shape, batching, category mapping, offsets, local merge, retries (no network)."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable

import httpx
import pytest
import respx
from azure.core.credentials import AccessToken

from voicecrm.adapters.pii.azure_language import (
    API_VERSION,
    DEFAULT_PII_CATEGORIES,
    LANGUAGE_SCOPE,
    AzureLanguageError,
    AzureLanguagePiiDetector,
    map_azure_category,
)
from voicecrm.ports.pii import PII_CATEGORIES, PiiPort

ENDPOINT = "https://voicecrm-language.cognitiveservices.azure.com"
URL = f"{ENDPOINT}/language/:analyze-text"


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str, **_kw: object) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken("fake-entra-token", int(time.time()) + 3600)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def entity(text: str, needle: str, category: str, score: float = 0.95, *, shift: int = 0, nth: int = 0) -> dict:
    pos = -1
    for _ in range(nth + 1):
        pos = text.index(needle, pos + 1)
    return {"text": needle, "category": category, "offset": pos + shift, "length": len(needle), "confidenceScore": score}


def service(entities_for: Callable[[str], list[dict]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        documents = [{"id": d["id"], "entities": entities_for(d["text"]), "warnings": []}
                     for d in body["analysisInput"]["documents"]]
        return httpx.Response(200, json={"kind": "PiiEntityRecognitionResults", "results": {
            "documents": documents, "errors": [], "modelVersion": "2025-11-01"}})

    return handler


def _detector(**kw) -> AzureLanguagePiiDetector:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("key", "language-key-123")
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


def _payload(route: respx.Route, index: int = -1) -> dict:
    return json.loads(route.calls[index].request.content)


def test_implements_the_port():
    assert isinstance(_detector(), PiiPort) and _detector().name == "azure_language"


async def test_request_shape_with_key_auth(api):
    route = api.post(URL).mock(side_effect=service(lambda text: []))
    out = await _detector().detect_many(["My name is Jane Doe.", "Hola"], language="en")
    assert out == [[], []]
    request = route.calls.last.request
    assert request.method == "POST" and request.url.path == "/language/:analyze-text"
    assert request.url.params["api-version"] == API_VERSION == "2025-11-01"
    assert request.headers["Ocp-Apim-Subscription-Key"] == "language-key-123" and "authorization" not in request.headers
    assert _payload(route) == {
        "kind": "PiiEntityRecognition",
        "parameters": {"modelVersion": "latest", "stringIndexType": "UnicodeCodePoint", "loggingOptOut": True,
                       "piiCategories": list(DEFAULT_PII_CATEGORIES)},
        "analysisInput": {"documents": [{"id": "0", "language": "en", "text": "My name is Jane Doe."},
                                        {"id": "1", "language": "en", "text": "Hola"}]},
    }


async def test_entra_sends_bearer_token(api):
    route = api.post(URL).mock(side_effect=service(lambda text: []))
    cred = FakeCredential()
    await _detector(auth="entra", key=None, credential=cred).detect_many(["hello"], language="en")
    assert route.calls.last.request.headers["Authorization"] == "Bearer fake-entra-token"
    assert "Ocp-Apim-Subscription-Key" not in route.calls.last.request.headers
    assert cred.scopes == [(LANGUAGE_SCOPE,)]


async def test_concurrency_can_be_changed_while_the_detector_is_in_use(api):
    api.post(URL).mock(side_effect=service(lambda text: []))
    detector = _detector(concurrency=2)
    assert detector.concurrency == 2
    assert detector.set_concurrency(12) == 12 and detector.concurrency == 12
    assert await detector.detect_many(["hello"] * 7, language="en") == [[]] * 7       # the new limit serves requests
    assert detector.set_concurrency(0) == 1                                           # never below one request at a time


async def test_entra_rejects_regional_endpoint():
    d = _detector(endpoint="https://westus.api.cognitive.microsoft.com", auth="entra", key=None,
                  credential=FakeCredential())
    with pytest.raises(AzureLanguageError, match="custom-domain endpoint"):
        await d.detect_many(["hello"], language="en")


async def test_texts_are_batched_by_five_and_results_stay_aligned(api):
    route = api.post(URL).mock(side_effect=service(
        lambda text: [entity(text, m.group(0), "Email") for m in re.finditer(r"\S+@\S+", text)]))
    texts = [f"Write to person{i}@example.com about fund {i}." for i in range(12)]
    out = await _detector(concurrency=2).detect_many(texts, language="en")
    assert sorted(len(_payload(route, i)["analysisInput"]["documents"]) for i in range(route.call_count)) == [2, 5, 5]
    for i, spans in enumerate(out):
        assert [(s.category, s.text, s.source) for s in spans] == [("personal_contact", f"person{i}@example.com",
                                                                    "azure_language")]
        assert texts[i][spans[0].start:spans[0].end] == spans[0].text


async def test_blank_texts_never_reach_the_service(api):
    route = api.post(URL).mock(side_effect=service(lambda text: []))
    assert await _detector().detect_many(["", "   ", "\n"], language="en") == [[], [], []]
    assert await _detector().detect_many([], language="en") == []
    assert route.call_count == 0
    out = await _detector().detect_many(["", "hello there", ""], language="en")
    assert out == [[], [], []] and _payload(route)["analysisInput"]["documents"] == [
        {"id": "0", "language": "en", "text": "hello there"}]


@pytest.mark.parametrize("azure,ours", [
    ("CreditCardNumber", "payment_card"), ("EUDebitCardNumber", "payment_card"), ("CVV", "payment_card"),
    ("ABARoutingNumber", "bank_account"), ("BankAccountNumber", "bank_account"), ("USBankAccountNumber", "bank_account"),
    ("InternationalBankingAccountNumber", "bank_account"), ("SortCode", "bank_account"), ("SWIFTCode", "bank_account"),
    ("CABankAccountNumber", "bank_account"),
    ("USSocialSecurityNumber", "government_id"), ("UKNationalInsuranceNumber", "government_id"),
    ("ESDNI", "government_id"), ("ESSocialSecurityNumber", "government_id"), ("PassportNumber", "government_id"),
    ("DriversLicenseNumber", "government_id"), ("FRNationalID", "government_id"),
    ("CASocialInsuranceNumber", "government_id"), ("UKNationalHealthNumber", "government_id"),
    ("BENationalNumberV2", "government_id"), ("INUniqueIdentificationNumber", "government_id"),
    ("NZInlandRevenueNumber", "government_id"), ("CZPersonalIdentityV2", "government_id"),
    ("AUBusinessNumber", None), ("BRLegalEntityNumber", None), ("DEValueAddedNumber", None),
    ("PLREGONNumber", None),
    ("Password", "credentials"), ("AzureStorageAccountKey", "credentials"), ("SQLServerConnectionString", "credentials"),
    ("AzureSAS", "credentials"),
    ("PhoneNumber", "personal_contact"), ("Email", "personal_contact"), ("Address", "personal_contact"),
    ("Person", None), ("Organization", None), ("URL", None), ("IPAddress", None), ("Date", None), ("DateTime", None),
    ("Age", None), ("DateOfBirth", None), ("ZipCode", None), ("VehicleIdentificationNumber", None),
    ("UKValueAddedTaxNumber", None), ("", None),
])
def test_category_mapping(azure, ours):
    assert map_azure_category(azure) == ours
    assert ours is None or ours in PII_CATEGORIES


def test_extra_category_map_overrides():
    assert map_azure_category("Person", {"Person": "personal_contact"}) == "personal_contact"
    assert map_azure_category("CreditCardNumber", {"CreditCardNumber": "bank_account"}) == "bank_account"


async def test_only_policed_categories_become_spans_and_confidence_is_kept(api):
    text = "Jane Doe of Contoso, jane@contoso.com, SSN 123-45-6789, card 4111 1111 1111 1111, call 206-555-0100."
    api.post(URL).mock(side_effect=service(lambda t: [
        entity(t, "Jane Doe", "Person"), entity(t, "Contoso", "Organization"),
        entity(t, "jane@contoso.com", "Email", 0.8), entity(t, "123-45-6789", "USSocialSecurityNumber", 0.65),
        entity(t, "4111 1111 1111 1111", "CreditCardNumber", 0.99), entity(t, "206-555-0100", "PhoneNumber", 0.9)]))
    (spans,) = await _detector().detect_many([text], language="en")
    assert [(s.category, s.text, s.confidence) for s in spans] == [
        ("personal_contact", "jane@contoso.com", 0.8), ("government_id", "123-45-6789", 0.65),
        ("payment_card", "4111 1111 1111 1111", 0.99), ("personal_contact", "206-555-0100", 0.9)]
    assert all(text[s.start:s.end] == s.text for s in spans)


async def test_offsets_are_unicode_code_points(api):
    text = "😀😀 my SSN is 123-45-6789 😀"
    api.post(URL).mock(side_effect=service(lambda t: [entity(t, "123-45-6789", "USSocialSecurityNumber")]))
    (spans,) = await _detector().detect_many([text], language="en")
    # code points: the emoji count once each (UTF-16 units would put the SSN at 15)
    assert [(s.start, s.end, s.text) for s in spans] == [(13, 24, "123-45-6789")]


async def test_misreported_offsets_are_relocated_by_value_or_dropped(api):
    text = "😀😀 card 4111 1111 1111 1111, again 4111 1111 1111 1111"
    def wrong(t):
        return [entity(t, "4111 1111 1111 1111", "CreditCardNumber", shift=2, nth=0),          # UTF-16-style +2
                {"text": "ghost", "category": "Email", "offset": 3, "length": 5, "confidenceScore": 0.9},
                {"text": "4111 1111 1111 1111", "category": "CreditCardNumber", "offset": 9999, "length": 19,
                 "confidenceScore": 0.9}]
    api.post(URL).mock(side_effect=service(wrong))
    (spans,) = await _detector().detect_many([text], language="en")
    number = "4111 1111 1111 1111"
    assert [(s.category, s.start, s.text) for s in spans] == [("payment_card", 8, number), ("payment_card", 35, number)]


async def test_long_texts_are_chunked_and_offsets_shifted_back(api):
    filler = "palabra " * 1500                                      # 12,000 characters
    text = filler + "mi SSN es 123-45-6789 y listo. " + filler
    route = api.post(URL).mock(side_effect=service(
        lambda t: [entity(t, "123-45-6789", "USSocialSecurityNumber")] if "123-45-6789" in t else []))
    (spans,) = await _detector().detect_many([text], language="es")
    docs = [d for i in range(route.call_count) for d in _payload(route, i)["analysisInput"]["documents"]]
    assert len(docs) >= 5 and all(len(d["text"]) <= 5120 for d in docs) and {d["language"] for d in docs} == {"es"}
    assert "".join(d["text"] for d in docs) == text                  # nothing lost, nothing duplicated
    assert [(s.category, s.text) for s in spans] == [("government_id", "123-45-6789")]
    assert text[spans[0].start:spans[0].end] == "123-45-6789"


@pytest.mark.parametrize("given,sent", [("es-US", "es"), ("en-GB", "en"), ("es", "es"), ("EN", "en"), ("fr", "en"),
                                        ("", "en")])
async def test_language_codes_are_normalised(api, given, sent):
    route = api.post(URL).mock(side_effect=service(lambda t: []))
    await _detector().detect_many(["hola"], language=given)
    assert _payload(route)["analysisInput"]["documents"][0]["language"] == sent


async def test_local_findings_for_cards_and_credentials_are_merged_by_default(api):
    api.post(URL).mock(side_effect=service(lambda t: []))
    spoken = "my card is four one one one one one one one one one one one one one one one and my password is hunter2 ok"
    email = "write to jane.doe@example.com"
    default = await _detector(merge_local=True).detect_many([spoken, email], language="en")
    assert [(s.category, s.source) for s in default[0]] == [("payment_card", "local"), ("credentials", "local")]
    assert default[1] == []                                          # contact info is Azure's job by default
    everything = await _detector(merge_local=True, merge_local_categories=None).detect_many([email], language="en")
    assert [s.category for s in everything[0]] == ["personal_contact"]
    off = await _detector(merge_local=False).detect_many([spoken], language="en")
    assert off == [[]]


async def test_azure_and_local_spans_for_the_same_number_are_unioned(api):
    text = "my card number is 4111 1111 1111 1111 thanks"
    api.post(URL).mock(side_effect=service(lambda t: [entity(t, "4111 1111 1111", "CreditCardNumber", 0.7)]))
    (spans,) = await _detector(merge_local=True).detect_many([text], language="en")
    assert [(s.category, s.text, s.source) for s in spans] == [("payment_card", "4111 1111 1111 1111", "azure_language")]
    assert text[spans[0].start:spans[0].end] == spans[0].text


async def test_stronger_span_wins_when_categories_conflict(api):
    text = "reference 123456789 here"
    api.post(URL).mock(side_effect=service(lambda t: [entity(t, "123456789", "ABARoutingNumber", 0.4),
                                                      entity(t, "123456789", "USSocialSecurityNumber", 0.8)]))
    (spans,) = await _detector().detect_many([text], language="en")
    assert [(s.category, s.confidence) for s in spans] == [("government_id", 0.8)]


async def test_min_confidence_filters_weak_entities(api):
    api.post(URL).mock(side_effect=service(lambda t: [entity(t, "a@b.com", "Email", 0.3),
                                                      entity(t, "c@d.com", "Email", 0.9)]))
    (spans,) = await _detector(min_confidence=0.5).detect_many(["a@b.com c@d.com"], language="en")
    assert [s.text for s in spans] == ["c@d.com"]


async def test_custom_pii_categories_and_none(api):
    route = api.post(URL).mock(side_effect=service(lambda t: []))
    await _detector(pii_categories=["All"]).detect_many(["x"], language="en")
    assert _payload(route)["parameters"]["piiCategories"] == ["All"]
    await _detector(pii_categories=None).detect_many(["x"], language="en")
    assert "piiCategories" not in _payload(route)["parameters"]


async def test_throttling_is_retried_with_retry_after(api):
    sleeps = Sleeps()
    route = api.post(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": "TooManyRequests", "message": "slow"}}),
        service(lambda t: [entity(t, "a@b.com", "Email")])(httpx.Request("POST", URL, content=json.dumps(
            {"analysisInput": {"documents": [{"id": "0", "text": "a@b.com"}]}}))),
    ])
    (spans,) = await _detector(sleep=sleeps).detect_many(["a@b.com"], language="en")
    assert route.call_count == 2 and sleeps.delays == [3.0] and [s.text for s in spans] == ["a@b.com"]


async def test_persistent_failures_raise_clear_errors(api):
    route = api.post(URL).mock(return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable",
                                                                           "message": "down"}}))
    with pytest.raises(AzureLanguageError, match="HTTP 503 ServiceUnavailable: down") as info:
        await _detector(max_retries=1).detect_many(["hello"], language="en")
    assert route.call_count == 2 and info.value.retryable and info.value.status_code == 503


async def test_auth_errors_name_the_fix(api):
    api.post(URL).mock(return_value=httpx.Response(401, json={"error": {"code": "401", "message": "bad token"}}))
    with pytest.raises(AzureLanguageError, match="Cognitive Services Language Reader"):
        await _detector().detect_many(["hello"], language="en")


async def test_document_errors_fail_closed_without_echoing_text(api, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SSN 123-45-6789 SECRET-TEXT"
    api.post(URL).mock(return_value=httpx.Response(200, json={"kind": "PiiEntityRecognitionResults", "results": {
        "documents": [], "errors": [{"id": "0", "error": {"code": "InvalidDocument", "message": "Document text is empty."}}],
        "modelVersion": "x"}}))
    with pytest.raises(AzureLanguageError, match="InvalidDocument") as info:
        await _detector().detect_many([secret], language="en")
    assert "SECRET-TEXT" not in str(info.value) and "SECRET-TEXT" not in caplog.text and "123-45" not in caplog.text


async def test_missing_document_in_response_is_an_error(api):
    api.post(URL).mock(return_value=httpx.Response(200, json={"kind": "PiiEntityRecognitionResults", "results": {
        "documents": [], "errors": [], "modelVersion": "x"}}))
    with pytest.raises(AzureLanguageError, match="missing document 0"):
        await _detector().detect_many(["hello"], language="en")


async def test_owned_client_is_created_lazily_and_closed(api):
    api.post(URL).mock(side_effect=service(lambda t: []))
    async with AzureLanguagePiiDetector(endpoint=ENDPOINT, key="k", auth="key", merge_local=False) as d:
        await d.detect_many(["hello"], language="en")
        client = d._client
        assert client is not None and not client.is_closed
    assert client.is_closed


def test_configuration_errors_are_clear():
    with pytest.raises(ValueError, match="endpoint"):
        AzureLanguagePiiDetector(endpoint="", key="k", auth="key")
    with pytest.raises(ValueError, match="requires an API key"):
        AzureLanguagePiiDetector(endpoint=ENDPOINT, auth="key", key=None)

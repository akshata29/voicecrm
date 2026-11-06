"""Azure AI Language PII detection (analyze-text, kind ``PiiEntityRecognition``, GA api-version 2025-11-01).

``POST {endpoint}/language/:analyze-text?api-version=2025-11-01`` with up to 5 documents per request and 5,120
characters per document (longer texts are split at whitespace and offsets are shifted back). Documents carry the
language code (``en`` / ``es``). ``stringIndexType=UnicodeCodePoint`` makes the service return offsets in Unicode
code points, i.e. directly usable as Python ``str`` indices; as a safety net an entity whose text does not match the
slice at its offset is re-located by value (nearest occurrence) and dropped if it cannot be placed.

Azure categories are mapped onto our ``PII_CATEGORIES``; ones we do not police (Person, Organization, URL, Date, ...)
are ignored on purpose: names belong in CRM notes. Medical / family-health semantics belong to the notes-policy engine.
Spoken digit sequences and credentials are weak spots of the cloud model, so by default the local detector's
``payment_card`` and ``credentials`` findings are merged in (``merge_local``).

Auth: Entra bearer (scope ``https://cognitiveservices.azure.com/.default``; custom-domain endpoint required) or
``Ocp-Apim-Subscription-Key``. Failures raise (fail closed): silently skipping PII detection would leak.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal, Self

import httpx

from ...ports.pii import PiiPort, PiiSpan
from ..transcription._azure_common import (
    RETRYABLE_STATUS,
    AzureAuth,
    AzureServiceError,
    Sleep,
    endpoint_host,
    error_details,
    send_with_retry,
)
from .local import LocalPiiDetector
from .packing import Document, distribute, plan_documents

logger = logging.getLogger(__name__)

API_VERSION = "2025-11-01"
LANGUAGE_SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_DOCUMENTS_PER_REQUEST = 5
MAX_CHARS_PER_DOCUMENT = 5120
# "Default" plus the preview-era categories that are not returned by default but that we need (all valid in the GA enum).
DEFAULT_PII_CATEGORIES = ("Default", "BankAccountNumber", "PassportNumber", "DriversLicenseNumber", "SortCode")
_SUPPORTED_LANGUAGES = {"en", "es"}
_REGIONAL_HOST_SUFFIXES = (".cognitive.microsoft.com",)
_PRIORITY = {"payment_card": 0, "government_id": 1, "bank_account": 2, "credentials": 3, "personal_contact": 4}


class AzureLanguageError(AzureServiceError):
    """PII detection through Azure AI Language failed (config, throttling, service or per-document errors)."""


# ---- category mapping -----------------------------------------------------------------------------------------------

_EXPLICIT_CATEGORIES = {
    "CreditCardNumber": "payment_card", "EUDebitCardNumber": "payment_card", "CVV": "payment_card",
    "CardVerificationValue": "payment_card",
    "ABARoutingNumber": "bank_account", "BankAccountNumber": "bank_account",
    "InternationalBankingAccountNumber": "bank_account", "SortCode": "bank_account", "SWIFTCode": "bank_account",
    "PhoneNumber": "personal_contact", "Email": "personal_contact", "Address": "personal_contact",
    "Password": "credentials",
}
_PATTERN_CATEGORIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bank(?:ing)?AccountNumber$"), "bank_account"),
    (re.compile(r"^(?:Azure|SQLServer)|ConnectionString|AuthKey|SAS$|StorageAccountKey|Password"), "credentials"),
    (re.compile(
        r"SocialSecurity|SocialInsurance|NationalID|NationalIdentification|IdentityCard|IdentityNumber|"
        r"PersonalIdentification|TaxIdentification|TaxIDNumber|TaxFileNumber|IndividualTaxpayer|UniqueTaxpayer|"
        r"PassportNumber|DriversLicense|ResidentRegistration|ResidentIdentity|ResidenceCard|ResidentCertificate|"
        r"CitizenCard|CitizensService|PersonalCode|PersonalNumber|PersonalNumericalCode|UniformCivilNumber|"
        r"UniqueMasterCitizen|FiscalCode|MyNumber|CPFNumber|NationalIDRG|DNI$|ElectoralRoll|PopulationIdentification|"
        r"NationalHealthNumber|HealthInsuranceNumber|HealthServiceNumber|EuropeanHealthNumber|"
        r"PersonalHealthIdentification|MedicareBeneficiary|MedicalAccountNumber|PersonalPublicService|"
        r"ZAIdentificationNumber|UkPassport|USUKPassport|NationalInsurance|NationalNumber|PersonalIdentity|"
        r"PermanentAccount|UniqueIdentification|InlandRevenue|MinistryOfHealth|SocialWelfare|UnifiedMultiPurposeID"),
     "government_id"),
)


def map_azure_category(category: str, extra: Mapping[str, str] | None = None) -> str | None:
    """Our PII category for an Azure entity category, or None when we deliberately do not police it."""
    if extra and category in extra:
        return extra[category]
    if category in _EXPLICIT_CATEGORIES:
        return _EXPLICIT_CATEGORIES[category]
    for pattern, ours in _PATTERN_CATEGORIES:
        if pattern.search(category):
            return ours
    return None


class AzureLanguagePiiDetector(PiiPort):
    name = "azure_language"

    def __init__(
        self,
        *,
        endpoint: str,
        key: str | None = None,
        auth: Literal["entra", "key"] = "entra",
        credential: Any | None = None,
        api_version: str = API_VERSION,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        jitter: float = 0.1,
        sleep: Sleep = asyncio.sleep,
        batch_size: int = MAX_DOCUMENTS_PER_REQUEST,
        max_chars: int = MAX_CHARS_PER_DOCUMENT,
        pack_chars: int = 0,
        concurrency: int = 4,
        pii_categories: Iterable[str] | None = DEFAULT_PII_CATEGORIES,
        model_version: str = "latest",
        min_confidence: float = 0.0,
        extra_category_map: Mapping[str, str] | None = None,
        merge_local: bool = True,
        local_detector: LocalPiiDetector | None = None,
        merge_local_categories: Iterable[str] | None = ("payment_card", "credentials"),
    ) -> None:
        """``merge_local_categories=None`` merges every local finding; ``merge_local=False`` uses Azure alone.

        ``pack_chars`` > 0 packs consecutive texts (transcript segments) into documents of at most that many characters,
        which cuts requests and billed text records about fivefold (see packing.py); 0 sends one document per text.
        """
        if not endpoint:
            raise ValueError("AzureLanguagePiiDetector needs the Language resource endpoint (VOICECRM_LANGUAGE_ENDPOINT)")
        self._endpoint = endpoint.rstrip("/")
        self._auth = AzureAuth(service="Azure Language", mode=auth, key=key, scope=LANGUAGE_SCOPE,
                               key_header="Ocp-Apim-Subscription-Key", credential=credential)
        self._api_version = api_version
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._jitter = jitter
        self._sleep = sleep
        self._batch_size = max(1, min(batch_size, MAX_DOCUMENTS_PER_REQUEST))
        self._max_chars = max(100, min(max_chars, MAX_CHARS_PER_DOCUMENT))
        self._pack_chars = max(0, min(pack_chars, self._max_chars))
        self._concurrency = max(1, concurrency)
        self._sem = asyncio.Semaphore(self._concurrency)
        self._categories = list(pii_categories) if pii_categories else None
        self._model_version = model_version
        self._min_confidence = min_confidence
        self._extra_map = dict(extra_category_map or {})
        self._local = (local_detector or LocalPiiDetector()) if merge_local else None
        self._merge_categories = set(merge_local_categories) if merge_local_categories is not None else None
        self._warned_languages: set[str] = set()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- PiiPort ----------------------------------------------------------------------------------------------
    async def detect_many(self, texts: list[str], *, language: str) -> list[list[PiiSpan]]:
        if self._auth.mode == "entra" and endpoint_host(self._endpoint).endswith(_REGIONAL_HOST_SUFFIXES):
            raise AzureLanguageError(
                "Entra authentication needs the Language resource's custom-domain endpoint "
                "(https://<name>.cognitiveservices.azure.com), not a regional endpoint; fix VOICECRM_LANGUAGE_ENDPOINT "
                "or set VOICECRM_AZURE_AUTH=key", service="Azure Language")
        lang = self._service_language(language)
        documents = plan_documents(texts, max_chars=self._max_chars, pack_chars=self._pack_chars)
        batches = [documents[i:i + self._batch_size] for i in range(0, len(documents), self._batch_size)]
        logger.info("azure language PII: %d texts -> %d documents (%d text records) in %d request(s)",
                    len(texts), len(documents), sum(d.records for d in documents), len(batches))
        per_text: list[list[PiiSpan]] = [[] for _ in texts]
        for placed in await asyncio.gather(*(self._analyze(batch, lang) for batch in batches)):
            for index, span in placed:
                per_text[index].append(span)
        local: list[list[PiiSpan]] = [[] for _ in texts]
        if self._local is not None:
            local = await self._local.detect_many(texts, language=language)
        return [
            _merge(found, [s for s in extra if self._merge_categories is None or s.category in self._merge_categories], text)
            for text, found, extra in zip(texts, per_text, local, strict=True)
        ]

    def text_records(self, texts: list[str]) -> int:
        """Text records the service bills for analysing these texts with this detector's packing."""
        return sum(d.records for d in plan_documents(texts, max_chars=self._max_chars, pack_chars=self._pack_chars))

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def set_concurrency(self, limit: int) -> int:
        """Change how many requests run at once. Requests already holding a permit finish under the old limit."""
        self._concurrency = max(1, int(limit))
        self._sem = asyncio.Semaphore(self._concurrency)
        return self._concurrency

    # ---- HTTP -------------------------------------------------------------------------------------------------
    async def _analyze(self, batch: list[Document], lang: str) -> list[tuple[int, PiiSpan]]:
        parameters: dict[str, Any] = {"modelVersion": self._model_version, "stringIndexType": "UnicodeCodePoint",
                                      "loggingOptOut": True}
        if self._categories:
            parameters["piiCategories"] = self._categories
        payload = {
            "kind": "PiiEntityRecognition",
            "parameters": parameters,
            "analysisInput": {"documents": [{"id": str(n), "language": lang, "text": document.text}
                                            for n, document in enumerate(batch)]},
        }
        async with self._sem:
            response = await send_with_retry(
                lambda: self._post(payload), service="Azure Language", max_retries=self._max_retries,
                backoff_base=self._backoff_base, backoff_cap=self._backoff_cap, sleep=self._sleep, jitter=self._jitter)
        if response.status_code != 200:
            raise self._error(response)
        try:
            results = response.json().get("results") or {}
        except (ValueError, AttributeError) as exc:
            raise AzureLanguageError("Azure Language returned an unreadable response", service="Azure Language",
                                     status_code=response.status_code) from exc
        failed = results.get("errors") or []
        if failed:
            codes = sorted({str((e.get("error") or {}).get("code") or "unknown") for e in failed if isinstance(e, dict)})
            raise AzureLanguageError(
                f"Azure Language could not analyse {len(failed)} of {len(batch)} document(s) ({', '.join(codes)}); "
                "refusing to continue without PII detection", service="Azure Language", code=codes[0] if codes else None)
        documents = {str(d.get("id")): d for d in results.get("documents") or [] if isinstance(d, dict)}
        out: list[tuple[int, PiiSpan]] = []
        for n, packed in enumerate(batch):
            document = documents.get(str(n))
            if document is None:
                raise AzureLanguageError(f"Azure Language response is missing document {n}", service="Azure Language")
            out.extend(distribute(packed, self._entity_spans(document.get("entities") or [], packed.text, 0)))
        return out

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        client = self._http()
        return await client.post(
            f"{self._endpoint}/language/:analyze-text", params={"api-version": self._api_version},
            headers=await self._auth.headers(), json=payload)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=10.0))
        return self._client

    def _error(self, response: httpx.Response) -> AzureLanguageError:
        code, message = error_details(response)
        status = response.status_code
        hint = {
            401: "authentication failed - with Entra assign 'Cognitive Services Language Reader' (or 'Cognitive "
                 "Services User') on the resource and use its custom-domain endpoint; with a key check "
                 "VOICECRM_LANGUAGE_KEY",
            403: "access denied - check the identity's role on the Language resource and its network rules",
            429: "throttled after retries - the Language resource tier limits requests per second",
        }.get(status)
        text = f"Azure Language PII request failed: HTTP {status}" + (f" {code}" if code else "") + f": {message}"
        if hint:
            text += f" ({hint})"
        return AzureLanguageError(text, service="Azure Language", status_code=status, code=code,
                                  retryable=status in RETRYABLE_STATUS)

    # ---- mapping ----------------------------------------------------------------------------------------------
    def _service_language(self, language: str) -> str:
        base = (language or "en").replace("_", "-").split("-")[0].lower()
        if base in _SUPPORTED_LANGUAGES:
            return base
        if base not in self._warned_languages:
            self._warned_languages.add(base)
            logger.warning("language %r is not configured for PII detection; analysing as English", base)
        return "en"

    def _entity_spans(self, entities: list[dict[str, Any]], chunk: str, base: int) -> list[PiiSpan]:
        spans: list[PiiSpan] = []
        for entity in entities:
            category = map_azure_category(str(entity.get("category") or ""), self._extra_map)
            confidence = float(entity.get("confidenceScore", 1.0))
            if category is None or confidence < self._min_confidence:
                continue
            located = _locate(chunk, int(entity.get("offset", 0)), int(entity.get("length", 0)), entity.get("text"))
            if located is None:
                logger.debug("dropping a %s entity whose offsets do not match the submitted text", category)
                continue
            start, end = located
            spans.append(PiiSpan(start=base + start, end=base + end, category=category, text=chunk[start:end],
                                 confidence=round(confidence, 2), source="azure_language"))
        return spans


def _locate(chunk: str, offset: int, length: int, entity_text: str | None) -> tuple[int, int] | None:
    """Offsets as reported when they match the entity text; else the nearest occurrence of the text; else None."""
    start, end = offset, offset + length
    if 0 <= start < end <= len(chunk) and (entity_text is None or chunk[start:end] == entity_text):
        return start, end
    if not entity_text:
        return None
    best: int | None = None
    pos = chunk.find(entity_text)
    while pos != -1:
        if best is None or abs(pos - offset) < abs(best - offset):
            best = pos
        pos = chunk.find(entity_text, pos + 1)
    return (best, best + len(entity_text)) if best is not None else None


def _merge(spans: list[PiiSpan], extra: list[PiiSpan], text: str) -> list[PiiSpan]:
    """Non-overlapping spans: the strongest evidence wins; overlapping spans of one category are unioned."""
    ordered = sorted([*spans, *extra], key=lambda s: (-s.confidence, -(s.end - s.start), _PRIORITY.get(s.category, 9),
                                                      s.start))
    kept: list[PiiSpan] = []
    for span in ordered:
        clash = next((k for k in kept if span.start < k.end and k.start < span.end), None)
        if clash is None:
            kept.append(span)
        elif clash.category == span.category:
            kept[kept.index(clash)] = clash.model_copy(update={
                "start": min(clash.start, span.start), "end": max(clash.end, span.end),
                "source": "azure_language" if "azure_language" in (clash.source, span.source) else clash.source,
            })
    merged: list[PiiSpan] = []
    for span in sorted(kept, key=lambda s: s.start):
        if merged and span.start < merged[-1].end:
            if merged[-1].category == span.category:
                merged[-1] = merged[-1].model_copy(update={"end": max(merged[-1].end, span.end)})
            continue
        merged.append(span)
    return [s if s.text == text[s.start:s.end] else s.model_copy(update={"text": text[s.start:s.end]}) for s in merged]

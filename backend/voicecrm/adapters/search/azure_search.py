"""Azure AI Search adapter for the product catalogue (REST via httpx, data-plane api-version 2026-04-01).

* ``index(catalogue)``: creates/updates the index definition (``PUT /indexes('name')``), uploads one document per
  product with ``mergeOrUpload`` in batches (``POST .../docs/search.index``; busy documents, status 422/503, are
  retried), then deletes documents of products that are no longer in the catalogue, so it is a safe, idempotent
  "(re)build" that never takes the index offline.
* ``search(query, top=, business_line=)``: ``POST .../docs/search.post.search`` with the simple query syntax (operator
  characters in the spoken/ASR query are neutralised), an optional ``business_lines`` filter and a field-weighted
  scoring profile (display name and aliases count more than strategy text).

Index design: lexical only (no vectors). Names use a custom analyzer (standard tokenizer, lower-case, ASCII folding,
Porter stemming) so "Equity Funds" matches "Equity Fund" and Spanish accents fold; ``share_class`` uses a keyword +
lower-case analyzer so the class letter ("C", "R6", "Advisor") matches exactly and stays filterable.

Auth: Entra bearer (scope ``https://search.azure.com/.default``; roles Search Service Contributor to manage the
index, Search Index Data Contributor to upload, Search Index Data Reader to query) or the ``api-key`` header.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal, Self

import httpx

from ...domain.catalogue import Catalogue, Product
from ...ports.search import CatalogueSearchPort, SearchHit
from ..transcription._azure_common import (
    RETRYABLE_STATUS,
    AzureAuth,
    AzureServiceError,
    Sleep,
    backoff_delay,
    error_details,
    send_with_retry,
)

logger = logging.getLogger(__name__)

API_VERSION = "2026-04-01"
SEARCH_SCOPE = "https://search.azure.com/.default"
DEFAULT_INDEX_NAME = "voicecrm-catalogue"
MAX_UPLOAD_BATCH = 1000
_PAGE = 1000
_INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_NAMES_ANALYZER = "voicecrm_names"
_KEYWORD_ANALYZER = "voicecrm_keyword"
_SCORING_PROFILE = "voicecrm_product_boost"
_SEARCH_FIELDS = "display_name,fund_name,aliases,share_class,strategy"
_SELECT_FIELDS = ("id,family_id,display_name,fund_name,share_class,aliases,asset_class,strategy,region,vehicle,"
                  "business_lines,status")


class AzureSearchError(AzureServiceError):
    """Azure AI Search call failed (missing index, rejected documents, auth, throttling or service error)."""


def _field(
    name: str, kind: str = "Edm.String", *, key: bool = False, searchable: bool = False, filterable: bool = False,
    facetable: bool = False, sortable: bool = False, analyzer: str | None = None,
) -> dict[str, Any]:
    """Explicit attributes everywhere: string fields default to searchable/filterable/sortable/facetable otherwise."""
    field: dict[str, Any] = {"name": name, "type": kind, "key": key, "searchable": searchable, "filterable": filterable,
                             "facetable": facetable, "sortable": sortable, "retrievable": True}
    if analyzer:
        field["analyzer"] = analyzer
    return field


def index_definition(index_name: str = DEFAULT_INDEX_NAME) -> dict[str, Any]:
    """The catalogue index: lexical fields only, custom name analyzers and a field-weighted default scoring profile."""
    return {
        "name": index_name,
        "fields": [
            _field("id", key=True, filterable=True, sortable=True),
            _field("family_id", filterable=True),
            _field("display_name", searchable=True, analyzer=_NAMES_ANALYZER),
            _field("fund_name", searchable=True, analyzer=_NAMES_ANALYZER),
            _field("share_class", searchable=True, filterable=True, facetable=True, analyzer=_KEYWORD_ANALYZER),
            _field("aliases", "Collection(Edm.String)", searchable=True, analyzer=_NAMES_ANALYZER),
            _field("asset_class", searchable=True, filterable=True, facetable=True, analyzer=_NAMES_ANALYZER),
            _field("strategy", searchable=True, analyzer=_NAMES_ANALYZER),
            _field("region", filterable=True, facetable=True),
            _field("vehicle", filterable=True, facetable=True),
            _field("business_lines", "Collection(Edm.String)", filterable=True, facetable=True),
            _field("status", filterable=True),
        ],
        "analyzers": [
            {"@odata.type": "#Microsoft.Azure.Search.CustomAnalyzer", "name": _NAMES_ANALYZER,
             "tokenizer": "standard_v2", "tokenFilters": ["lowercase", "asciifolding", "porter_stem"]},
            {"@odata.type": "#Microsoft.Azure.Search.CustomAnalyzer", "name": _KEYWORD_ANALYZER,
             "tokenizer": "keyword_v2", "tokenFilters": ["lowercase", "asciifolding"]},
        ],
        "scoringProfiles": [{"name": _SCORING_PROFILE, "text": {"weights": {
            "display_name": 3, "aliases": 2.5, "fund_name": 2, "share_class": 1, "strategy": 0.5}}}],
        "defaultScoringProfile": _SCORING_PROFILE,
    }


def product_document(product: Product) -> dict[str, Any]:
    return {
        "@search.action": "mergeOrUpload",
        "id": product.product_id,
        "family_id": product.family_id,
        "display_name": product.display_name,
        "fund_name": product.fund_name,
        "share_class": product.share_class,
        "aliases": [a for a in product.aliases if a],
        "asset_class": product.asset_class,
        "strategy": product.strategy,
        "region": product.region,
        "vehicle": product.vehicle,
        "business_lines": list(product.business_lines),
        "status": product.status,
    }


def _odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class AzureSearchCatalogue(CatalogueSearchPort):
    name = "azure_search"

    def __init__(
        self,
        *,
        endpoint: str,
        index_name: str = DEFAULT_INDEX_NAME,
        key: str | None = None,
        auth: Literal["entra", "key"] = "entra",
        credential: Any | None = None,
        api_version: str = API_VERSION,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        jitter: float = 0.1,
        sleep: Sleep = asyncio.sleep,
        batch_size: int = 500,
        include_unscoped: bool = True,
        prune_stale: bool = True,
        allow_index_downtime: bool = False,
    ) -> None:
        """``include_unscoped``: with a business-line filter, also return products that list no business lines."""
        if not endpoint:
            raise ValueError("AzureSearchCatalogue needs the search service endpoint (VOICECRM_SEARCH_ENDPOINT)")
        if not _INDEX_NAME_RE.match(index_name):
            raise ValueError(f"invalid Azure AI Search index name {index_name!r}: use lower-case letters, digits, "
                             "dashes and underscores, starting with a letter or digit")
        self._endpoint = endpoint.rstrip("/")
        self._index = index_name
        self._auth = AzureAuth(service="Azure AI Search", mode=auth, key=key, scope=SEARCH_SCOPE, key_header="api-key",
                               credential=credential)
        self._api_version = api_version
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._jitter = jitter
        self._sleep = sleep
        self._batch_size = max(1, min(batch_size, MAX_UPLOAD_BATCH))
        self._include_unscoped = include_unscoped
        self._prune = prune_stale
        self._allow_downtime = allow_index_downtime

    @property
    def index_url(self) -> str:
        return f"{self._endpoint}/indexes('{self._index}')"

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- index management -------------------------------------------------------------------------------------
    async def ensure_index(self) -> None:
        """Creates the index or updates its definition in place (idempotent)."""
        params = {"allowIndexDowntime": "true"} if self._allow_downtime else None
        response = await self._call("PUT", self.index_url, json=index_definition(self._index), params=params,
                                    headers={"Prefer": "return=representation"})
        if response.status_code not in (200, 201, 204):
            hint = ""
            if response.status_code == 400:
                hint = (" (the existing index cannot be updated in place - delete it, or pass "
                        "allow_index_downtime=True when only analyzers changed)")
            raise self._error(response, "create/update the index", hint)

    async def delete_index(self) -> None:
        response = await self._call("DELETE", self.index_url)
        if response.status_code not in (200, 204, 404):
            raise self._error(response, "delete the index")

    async def index(self, catalogue: Catalogue) -> int:
        await self.ensure_index()
        documents = [product_document(p) for p in catalogue.products]
        for start in range(0, len(documents), self._batch_size):
            await self._upload(documents[start:start + self._batch_size])
        removed = await self._prune_stale({d["id"] for d in documents}) if self._prune else 0
        logger.info("azure search index %s: %d documents uploaded, %d stale removed", self._index, len(documents), removed)
        return len(documents)

    async def _upload(self, documents: list[dict[str, Any]]) -> None:
        pending = documents
        attempt = 0
        while pending:
            response = await self._call("POST", f"{self.index_url}/docs/search.index", json={"value": pending})
            if response.status_code not in (200, 207):
                raise self._error(response, "upload documents")
            failed = [r for r in response.json().get("value", []) if not r.get("status")]
            if not failed:
                return
            fatal = [r for r in failed if r.get("statusCode") not in (422, 503)]
            if fatal:
                first = fatal[0]
                raise AzureSearchError(
                    f"Azure AI Search rejected {len(fatal)} document(s); first: key={first.get('key')} "
                    f"status={first.get('statusCode')} {str(first.get('errorMessage'))[:200]}",
                    service="Azure AI Search", status_code=first.get("statusCode"))
            attempt += 1
            if attempt > self._max_retries:
                raise AzureSearchError(
                    f"Azure AI Search stayed too busy: {len(failed)} document(s) were not indexed after "
                    f"{self._max_retries} retries", service="Azure AI Search", status_code=503, retryable=True)
            busy = {r["key"] for r in failed}
            pending = [d for d in pending if d["id"] in busy]
            delay = backoff_delay(attempt - 1, base=self._backoff_base, cap=self._backoff_cap, jitter=self._jitter)
            logger.warning("azure search busy: retrying %d document(s) in %.1fs", len(pending), delay)
            await self._sleep(delay)

    async def _prune_stale(self, keep: set[str]) -> int:
        """Deletes indexed documents whose id is not in ``keep`` (keyset paging: no 100k skip limit)."""
        stale: list[str] = []
        last: str | None = None
        while True:
            body: dict[str, Any] = {"search": "*", "select": "id", "orderby": "id asc", "top": _PAGE}
            if last is not None:
                body["filter"] = f"id gt {_odata_string(last)}"
            response = await self._call("POST", f"{self.index_url}/docs/search.post.search", json=body)
            if response.status_code != 200:
                raise self._error(response, "list indexed documents")
            page = [str(d["id"]) for d in response.json().get("value", [])]
            stale.extend(i for i in page if i not in keep)
            if len(page) < _PAGE:
                break
            last = page[-1]
        for start in range(0, len(stale), self._batch_size):
            batch = [{"@search.action": "delete", "id": i} for i in stale[start:start + self._batch_size]]
            response = await self._call("POST", f"{self.index_url}/docs/search.index", json={"value": batch})
            if response.status_code not in (200, 207):
                raise self._error(response, "delete stale documents")
        return len(stale)

    # ---- query ------------------------------------------------------------------------------------------------
    async def search(self, query: str, *, top: int = 10, business_line: str | None = None) -> list[SearchHit]:
        text = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", query or "")).strip()
        if not text:
            return []
        body: dict[str, Any] = {
            "search": text, "queryType": "simple", "searchMode": "any", "searchFields": _SEARCH_FIELDS,
            "select": _SELECT_FIELDS, "top": max(1, min(top, 1000)),
        }
        if business_line:
            clause = f"business_lines/any(b: b eq {_odata_string(business_line)})"
            body["filter"] = f"({clause} or not business_lines/any())" if self._include_unscoped else clause
        response = await self._call("POST", f"{self.index_url}/docs/search.post.search", json=body)
        if response.status_code == 404:
            raise AzureSearchError(
                f"Azure AI Search index {self._index!r} not found - build it first with AzureSearchCatalogue.index()",
                service="Azure AI Search", status_code=404)
        if response.status_code != 200:
            raise self._error(response, "search the catalogue")
        hits = []
        for doc in response.json().get("value", []):
            fields = {k: v for k, v in doc.items() if not k.startswith("@") and k != "id"}
            hits.append(SearchHit(id=str(doc["id"]), score=float(doc.get("@search.score", 0.0)), fields=fields))
        return hits

    # ---- HTTP -------------------------------------------------------------------------------------------------
    async def _call(
        self, method: str, url: str, *, json: Any = None, params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async def send() -> httpx.Response:
            return await self._http().request(
                method, url, json=json, params={"api-version": self._api_version, **(params or {})},
                headers={**await self._auth.headers(), **(headers or {})})

        return await send_with_retry(send, service="Azure AI Search", max_retries=self._max_retries,
                                     backoff_base=self._backoff_base, backoff_cap=self._backoff_cap,
                                     sleep=self._sleep, jitter=self._jitter)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=10.0))
        return self._client

    def _error(self, response: httpx.Response, action: str, hint: str = "") -> AzureSearchError:
        code, message = error_details(response)
        status = response.status_code
        extra = {
            401: " (authentication failed - with Entra the identity needs Search Index Data Reader/Contributor and "
                 "Search Service Contributor; with a key check VOICECRM_SEARCH_KEY)",
            403: " (access denied - check the role assignments and that role-based access control is enabled on the "
                 "search service)",
            429: " (throttled after retries - the search service is over its query or indexing limits)",
        }.get(status, "")
        return AzureSearchError(
            f"Azure AI Search could not {action}: HTTP {status}" + (f" {code}" if code else "") + f": {message}"
            + (hint or extra), service="Azure AI Search", status_code=status, code=code,
            retryable=status in RETRYABLE_STATUS)

"""AzureSearchCatalogue: index definition, batched uploads, stale pruning, query shape and error handling (no network)."""
from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from azure.core.credentials import AccessToken

from voicecrm.adapters.search.azure_search import (
    API_VERSION,
    SEARCH_SCOPE,
    AzureSearchCatalogue,
    AzureSearchError,
    index_definition,
    product_document,
)
from voicecrm.domain.catalogue import Catalogue, Product
from voicecrm.ports.search import CatalogueSearchPort

ENDPOINT = "https://voicecrm-search.search.windows.net"
INDEX = "voicecrm-catalogue"
INDEX_URL = f"{ENDPOINT}/indexes('{INDEX}')"
DOCS_INDEX = f"{INDEX_URL}/docs/search.index"
DOCS_SEARCH = f"{INDEX_URL}/docs/search.post.search"


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


def _product(n: int, share_class: str = "C", **kw) -> Product:
    values = {
        "product_id": f"SYN-{n:06d}", "family_id": f"FAM-{n // 3:04d}", "fund_name": "Contoso Synthetic Equity Fund",
        "share_class": share_class, "display_name": f"Contoso Synthetic Equity Fund Class {share_class}",
        "asset_class": "Equity", "strategy": "Large-cap growth", "region": "US",
        "business_lines": ["professional_buyer"],
        "aliases": ["Synthetic Equity Fund", "fondo sintético de renta variable"],
    }
    values.update(kw)
    return Product(**values)


def _catalogue(count: int = 5) -> Catalogue:
    return Catalogue(version="test", products=[_product(i, "AC"[i % 2]) for i in range(1, count + 1)])


def _search(**kw) -> AzureSearchCatalogue:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("key", "search-admin-key")
    kw.setdefault("auth", "key")
    kw.setdefault("http_client", httpx.AsyncClient(verify=False))
    kw.setdefault("sleep", Sleeps())
    kw.setdefault("jitter", 0)
    return AzureSearchCatalogue(**kw)


def _ok_results(request: httpx.Request) -> httpx.Response:
    docs = json.loads(request.content)["value"]
    return httpx.Response(200, json={"value": [{"key": d["id"], "status": True, "statusCode": 201} for d in docs]})


def _body(route: respx.Route, index: int = -1) -> dict:
    return json.loads(route.calls[index].request.content)


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def _empty_listing(api) -> respx.Route:
    return api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json={"value": []}))


def test_implements_the_port():
    assert isinstance(_search(), CatalogueSearchPort) and _search().name == "azure_search"


# ---- index definition -------------------------------------------------------------------------------------------

def test_index_definition_is_lexical_with_consistent_references():
    definition = index_definition(INDEX)
    fields = {f["name"]: f for f in definition["fields"]}
    assert definition["name"] == INDEX
    assert set(fields) == {"id", "family_id", "display_name", "fund_name", "share_class", "aliases", "asset_class",
                           "strategy", "region", "vehicle", "business_lines", "status"}
    assert [n for n, f in fields.items() if f["key"]] == ["id"] and fields["id"]["type"] == "Edm.String"
    assert not any("Single" in f["type"] or "dimensions" in f for f in fields.values())        # no vectors
    assert fields["aliases"]["type"] == "Collection(Edm.String)" and fields["aliases"]["searchable"]
    assert fields["business_lines"]["type"] == "Collection(Edm.String)"
    assert fields["business_lines"]["filterable"] and not fields["business_lines"]["searchable"]
    assert not fields["aliases"]["sortable"] and not fields["business_lines"]["sortable"]
    analyzers = {a["name"] for a in definition["analyzers"]}
    assert {f["analyzer"] for f in fields.values() if "analyzer" in f} <= analyzers
    assert all(f["searchable"] for f in fields.values() if "analyzer" in f)
    names = next(a for a in definition["analyzers"] if a["name"] == "voicecrm_names")
    assert names["@odata.type"] == "#Microsoft.Azure.Search.CustomAnalyzer" and names["tokenizer"] == "standard_v2"
    assert names["tokenFilters"] == ["lowercase", "asciifolding", "porter_stem"]
    profile = definition["scoringProfiles"][0]
    assert definition["defaultScoringProfile"] == profile["name"]
    assert set(profile["text"]["weights"]) <= {n for n, f in fields.items() if f["searchable"]}


def test_product_documents_map_every_indexed_field():
    doc = product_document(_product(7, "R6", aliases=["A", "", "B"], business_lines=["retail", "professional_buyer"]))
    assert doc["@search.action"] == "mergeOrUpload" and doc["id"] == "SYN-000007"
    assert doc["share_class"] == "R6" and doc["aliases"] == ["A", "B"]
    assert doc["business_lines"] == ["retail", "professional_buyer"] and doc["fund_name"] == "Contoso Synthetic Equity Fund"
    assert {"family_id", "display_name", "asset_class", "strategy", "region", "vehicle", "status"} <= set(doc)


# ---- indexing ---------------------------------------------------------------------------------------------------

async def test_index_creates_the_index_uploads_in_batches_and_returns_the_count(api):
    put = api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    upload = api.post(DOCS_INDEX).mock(side_effect=_ok_results)
    listing = _empty_listing(api)
    assert await _search(batch_size=2).index(_catalogue(5)) == 5
    request = put.calls.last.request
    assert request.url.params["api-version"] == API_VERSION == "2026-04-01"
    assert "allowIndexDowntime" not in request.url.params
    assert request.headers["api-key"] == "search-admin-key" and request.headers["Prefer"] == "return=representation"
    assert "authorization" not in request.headers
    assert _body(put) == index_definition(INDEX)
    assert [len(_body(upload, i)["value"]) for i in range(upload.call_count)] == [2, 2, 1]
    assert [d["id"] for i in range(3) for d in _body(upload, i)["value"]] == [f"SYN-{n:06d}" for n in range(1, 6)]
    assert listing.call_count == 1


async def test_entra_sends_a_bearer_token_with_the_search_scope(api):
    put = api.put(INDEX_URL).mock(return_value=httpx.Response(204))
    api.post(DOCS_INDEX).mock(side_effect=_ok_results)
    _empty_listing(api)
    cred = FakeCredential()
    await _search(auth="entra", key=None, credential=cred).index(_catalogue(1))
    request = put.calls.last.request
    assert request.headers["Authorization"] == "Bearer fake-entra-token" and "api-key" not in request.headers
    assert cred.scopes == [(SEARCH_SCOPE,)] and SEARCH_SCOPE == "https://search.azure.com/.default"


async def test_allow_index_downtime_is_opt_in(api):
    put = api.put(INDEX_URL).mock(return_value=httpx.Response(204))
    api.post(DOCS_INDEX).mock(side_effect=_ok_results)
    _empty_listing(api)
    await _search(allow_index_downtime=True).index(_catalogue(1))
    assert put.calls.last.request.url.params["allowIndexDowntime"] == "true"


async def test_busy_documents_are_retried_individually(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    upload = api.post(DOCS_INDEX).mock(side_effect=[
        httpx.Response(207, json={"value": [
            {"key": "SYN-000001", "status": True, "statusCode": 201},
            {"key": "SYN-000002", "status": False, "statusCode": 503, "errorMessage": "busy"}]}),
        httpx.Response(200, json={"value": [{"key": "SYN-000002", "status": True, "statusCode": 201}]}),
    ])
    _empty_listing(api)
    sleeps = Sleeps()
    assert await _search(sleep=sleeps).index(_catalogue(2)) == 2
    assert [d["id"] for d in _body(upload, 1)["value"]] == ["SYN-000002"] and sleeps.delays == [1.0]


async def test_rejected_documents_raise_with_the_first_error(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    api.post(DOCS_INDEX).mock(return_value=httpx.Response(207, json={"value": [
        {"key": "SYN-000001", "status": False, "statusCode": 400, "errorMessage": "field 'strategy' is too long"}]}))
    with pytest.raises(AzureSearchError, match="rejected 1 document.*SYN-000001.*400.*too long"):
        await _search().index(_catalogue(1))


async def test_documents_that_stay_busy_raise_after_the_retry_budget(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    upload = api.post(DOCS_INDEX).mock(return_value=httpx.Response(207, json={"value": [
        {"key": "SYN-000001", "status": False, "statusCode": 503, "errorMessage": "busy"}]}))
    with pytest.raises(AzureSearchError, match="too busy") as info:
        await _search(max_retries=2).index(_catalogue(1))
    assert upload.call_count == 3 and info.value.retryable


async def test_stale_documents_are_pruned_with_keyset_paging(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    upload = api.post(DOCS_INDEX).mock(side_effect=_ok_results)
    page_1 = [{"id": f"SYN-{n:06d}"} for n in range(1, 1001)]               # a full page: another page is requested
    page_2 = [{"id": "SYN-001001"}, {"id": "ZZZ-OLD-1"}]
    listing = api.post(DOCS_SEARCH).mock(side_effect=[httpx.Response(200, json={"value": page_1}),
                                                      httpx.Response(200, json={"value": page_2})])
    assert await _search().index(_catalogue(3)) == 3
    first, second = _body(listing, 0), _body(listing, 1)
    assert first == {"search": "*", "select": "id", "orderby": "id asc", "top": 1000}
    assert second["filter"] == "id gt 'SYN-001000'"
    deleted = [d["id"] for i in range(upload.call_count) for d in _body(upload, i)["value"]
               if d["@search.action"] == "delete"]
    assert deleted == [f"SYN-{n:06d}" for n in range(4, 1001)] + ["SYN-001001", "ZZZ-OLD-1"]


async def test_pruning_can_be_disabled(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(201, json={}))
    api.post(DOCS_INDEX).mock(side_effect=_ok_results)
    listing = _empty_listing(api)
    await _search(prune_stale=False).index(_catalogue(1))
    assert listing.call_count == 0


async def test_index_update_conflict_explains_the_options(api):
    api.put(INDEX_URL).mock(return_value=httpx.Response(400, json={"error": {"code": "", "message":
                                                                              "cannot change the analyzer"}}))
    with pytest.raises(AzureSearchError, match="cannot change the analyzer.*allow_index_downtime"):
        await _search().ensure_index()


async def test_delete_index_tolerates_a_missing_index(api):
    delete = api.delete(INDEX_URL).mock(return_value=httpx.Response(404, json={"error": {"message": "not found"}}))
    await _search().delete_index()
    assert delete.call_count == 1
    delete.mock(return_value=httpx.Response(204))
    await _search().delete_index()


# ---- query ------------------------------------------------------------------------------------------------------

HITS = {"value": [
    {"@search.score": 7.25, "id": "SYN-000002", "display_name": "Contoso Synthetic Equity Fund Class C",
     "share_class": "C", "business_lines": ["professional_buyer"], "fund_name": "Contoso Synthetic Equity Fund"},
    {"@search.score": 3.5, "id": "SYN-000001", "display_name": "Contoso Synthetic Equity Fund Class A",
     "share_class": "A"},
]}


async def test_search_request_shape_and_hit_parsing(api):
    route = api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json=HITS))
    hits = await _search().search("Contoso Equity Fund class C", top=3, business_line="professional_buyer")
    request = route.calls.last.request
    assert request.url.params["api-version"] == API_VERSION and request.headers["api-key"] == "search-admin-key"
    body = _body(route)
    assert body["search"] == "Contoso Equity Fund class C" and body["queryType"] == "simple"
    assert body["searchMode"] == "any" and body["top"] == 3
    assert body["searchFields"] == "display_name,fund_name,aliases,share_class,strategy"
    assert body["select"].startswith("id,family_id,display_name") and "business_lines" in body["select"]
    assert body["filter"] == ("(business_lines/any(b: b eq 'professional_buyer') or not business_lines/any())")
    assert [(h.id, h.score) for h in hits] == [("SYN-000002", 7.25), ("SYN-000001", 3.5)]
    assert hits[0].fields == {"display_name": "Contoso Synthetic Equity Fund Class C", "share_class": "C",
                              "business_lines": ["professional_buyer"], "fund_name": "Contoso Synthetic Equity Fund"}
    assert "id" not in hits[0].fields and not any(k.startswith("@") for k in hits[0].fields)


async def test_business_line_filter_can_exclude_unscoped_products_and_escapes_quotes(api):
    route = api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json={"value": []}))
    await _search(include_unscoped=False).search("equity", business_line="retail")
    assert _body(route)["filter"] == "business_lines/any(b: b eq 'retail')"
    await _search().search("equity", business_line="o'brien")
    assert "'o''brien'" in _body(route)["filter"]
    await _search().search("equity")
    assert "filter" not in _body(route)


@pytest.mark.parametrize("spoken,sent", [
    ('Contoso "Equity" Fund -class +C*', "Contoso Equity Fund class C"),
    ("equity|income (class see)~2", "equity income class see 2"),
    ("fondo   de  renta\nvariable, clase C.", "fondo de renta variable clase C"),
    ("S&P 500 / R6", "S P 500 R6"),
])
async def test_query_operator_characters_are_neutralised(api, spoken, sent):
    route = api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json={"value": []}))
    await _search().search(spoken)
    assert _body(route)["search"] == sent


async def test_empty_queries_return_nothing_without_a_request(api):
    route = api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json=HITS))
    for query in ("", "   ", "+-*()", None):
        assert await _search().search(query) == []                      # type: ignore[arg-type]
    assert route.call_count == 0


@pytest.mark.parametrize("top,sent", [(0, 1), (-5, 1), (10, 10), (5000, 1000)])
async def test_top_is_clamped(api, top, sent):
    route = api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json={"value": []}))
    await _search().search("equity", top=top)
    assert _body(route)["top"] == sent


async def test_missing_index_has_an_actionable_error(api):
    api.post(DOCS_SEARCH).mock(return_value=httpx.Response(404, json={"error": {"code": "", "message": "no index"}}))
    with pytest.raises(AzureSearchError, match="not found - build it first") as info:
        await _search().search("equity")
    assert info.value.status_code == 404


async def test_throttling_is_retried_and_auth_failures_name_the_fix(api):
    sleeps = Sleeps()
    route = api.post(DOCS_SEARCH).mock(side_effect=[httpx.Response(503, headers={"retry-after": "2"}, json={}),
                                                    httpx.Response(200, json=HITS)])
    assert len(await _search(sleep=sleeps).search("equity")) == 2 and sleeps.delays == [2.0]
    route.mock(return_value=httpx.Response(403, json={"error": {"code": "Forbidden", "message": "nope"}}))
    with pytest.raises(AzureSearchError, match="Forbidden: nope.*role assignments"):
        await _search().search("equity")
    route.mock(return_value=httpx.Response(401, json={"error": {"message": "bad token"}}))
    with pytest.raises(AzureSearchError, match="Search Index Data Reader"):
        await _search().search("equity")


# ---- construction -----------------------------------------------------------------------------------------------

def test_configuration_errors_are_clear():
    with pytest.raises(ValueError, match="endpoint"):
        AzureSearchCatalogue(endpoint="", key="k", auth="key")
    with pytest.raises(ValueError, match="invalid Azure AI Search index name"):
        AzureSearchCatalogue(endpoint=ENDPOINT, key="k", auth="key", index_name="Bad Name/1")
    with pytest.raises(ValueError, match="requires an API key"):
        AzureSearchCatalogue(endpoint=ENDPOINT, auth="key", key=None)


async def test_owned_client_is_created_lazily_and_closed(api):
    api.post(DOCS_SEARCH).mock(return_value=httpx.Response(200, json=HITS))
    async with AzureSearchCatalogue(endpoint=ENDPOINT, key="k", auth="key") as search:
        assert len(await search.search("equity")) == 2
        client = search._client
        assert client is not None and not client.is_closed
    assert client.is_closed

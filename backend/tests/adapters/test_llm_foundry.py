"""FoundryLlm: v1 request shape, auth modes, strict-schema fixups, parameter negotiation, retries, JSON repair, logging."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
import respx
from azure.core.credentials import AccessToken

from voicecrm.adapters.llm.errors import LlmOutputError, LlmRequestError
from voicecrm.adapters.llm.foundry import FOUNDRY_SCOPE, FoundryLlm, ensure_strict_schema
from voicecrm.ports.llm import LlmPort

ENDPOINT = "https://voicecrm-foundry.openai.azure.com"
CHAT = f"{ENDPOINT}/openai/v1/chat/completions"

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "tasks": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "tasks"],
    "additionalProperties": False,
}


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


def _completion(content: str | None, *, finish: str = "stop", usage: tuple[int, int] = (120, 30),
                cached: int | None = None, refusal: str | None = None) -> dict:
    body: dict = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini-2025-04-14",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, "refusal": refusal}}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }
    if cached is not None:
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": cached}
    return body


def _ok(payload: dict | None = None, **kw) -> httpx.Response:
    return httpx.Response(200, json=_completion(json.dumps(payload or {"summary": "ok", "tasks": []}), **kw))


def _error(status: int, message: str, *, param: str | None = None, code: str | None = None,
           headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={"error": {"message": message, "type": "invalid_request_error",
                                                                    "param": param, "code": code}})


def _body(route: respx.Route, index: int = -1) -> dict:
    return json.loads(route.calls[index].request.content)


def _llm(**kw) -> FoundryLlm:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("deployment", "gpt-41-mini")
    kw.setdefault("auth", "key")
    kw.setdefault("key", "foundry-key-123")
    kw.setdefault("http_client", httpx.AsyncClient(verify=False))
    kw.setdefault("sleep", Sleeps())
    kw.setdefault("jitter", 0)
    return FoundryLlm(**kw)


async def _ask(llm: FoundryLlm, **kw):
    kw.setdefault("system", "You write CRM notes.")
    kw.setdefault("user", "Summarise the call.")
    kw.setdefault("json_schema", SCHEMA)
    kw.setdefault("schema_name", "crm_note")
    return await llm.generate_json(**kw)


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_implements_the_port():
    llm = _llm()
    assert isinstance(llm, LlmPort) and llm.name == "foundry" and llm.model == "gpt-41-mini"


async def test_v1_request_shape_and_key_auth(api):
    route = api.post(CHAT).mock(return_value=_ok({"summary": "Met Cora", "tasks": ["send fees"]}))
    result = await _ask(_llm(), max_output_tokens=777, temperature=0.2)
    request = route.calls.last.request
    assert request.url == CHAT and "api-version" not in request.url.params
    assert request.headers["api-key"] == "foundry-key-123"
    assert request.headers["authorization"] == "Bearer foundry-key-123"
    body = _body(route)
    assert body["model"] == "gpt-41-mini"
    assert body["messages"] == [{"role": "system", "content": "You write CRM notes."},
                                {"role": "user", "content": "Summarise the call."}]
    assert body["max_completion_tokens"] == 777 and "max_tokens" not in body and body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "crm_note", "strict": True, "schema": SCHEMA}}
    assert result.data == {"summary": "Met Cora", "tasks": ["send fees"]}


async def test_entra_auth_uses_a_bearer_token_with_the_foundry_scope(api):
    route = api.post(CHAT).mock(return_value=_ok())
    cred = FakeCredential()
    llm = _llm(auth="entra", key=None, credential=cred)
    await _ask(llm)
    await _ask(llm)
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer fake-entra-token" and "api-key" not in request.headers
    assert cred.scopes == [(FOUNDRY_SCOPE,)] and FOUNDRY_SCOPE == "https://ai.azure.com/.default"


@pytest.mark.parametrize("endpoint", [ENDPOINT, ENDPOINT + "/", ENDPOINT + "/openai/v1", ENDPOINT + "/openai/v1/"])
async def test_endpoint_forms_resolve_to_the_v1_chat_route(api, endpoint):
    route = api.post(CHAT).mock(return_value=_ok())
    await _ask(_llm(endpoint=endpoint))
    assert route.call_count == 1


async def test_usage_includes_tokens_cached_tokens_latency_and_deployment_name(api):
    api.post(CHAT).mock(return_value=_ok(usage=(1500, 220), cached=1024))
    usage = (await _ask(_llm())).usage
    assert (usage.model, usage.tokens_in, usage.tokens_out, usage.cached_tokens) == ("gpt-41-mini", 1500, 220, 1024)
    assert usage.latency_ms >= 0


async def test_cached_tokens_absent_is_none(api):
    api.post(CHAT).mock(return_value=_ok())
    assert (await _ask(_llm())).usage.cached_tokens is None


async def test_schema_is_made_strict_before_sending(api):
    route = api.post(CHAT).mock(return_value=_ok({"summary": "s"}))
    loose = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1},
                                              "note": {"type": "string", "default": "x"}},
             "required": ["summary"]}
    await _ask(_llm(), json_schema=loose)
    sent = _body(route)["response_format"]["json_schema"]["schema"]
    assert sent["additionalProperties"] is False and sent["required"] == ["summary", "note"]
    assert sent["properties"]["summary"] == {"type": "string"}                 # minLength dropped
    assert sent["properties"]["note"] == {"type": ["string", "null"]}          # optional -> nullable, default dropped


async def test_schema_is_sent_untouched_when_strict_schema_is_off(api):
    route = api.post(CHAT).mock(return_value=_ok({"summary": "s"}))
    loose = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1}}, "required": ["summary"]}
    await _ask(_llm(strict_schema=False), json_schema=loose)
    sent = _body(route)["response_format"]["json_schema"]
    assert sent["schema"] == loose and sent["strict"] is False


def test_ensure_strict_schema_handles_nesting_refs_and_property_names():
    schema = {
        "type": "object", "title": "Proposal",
        "properties": {
            "format": {"type": "string", "pattern": "^x"},                    # a PROPERTY named like a keyword
            "default": {"type": "integer", "minimum": 0},
            "items": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/Item"}},
            "owner": {"$ref": "#/$defs/Owner"},
            "kind": {"type": "string", "enum": ["a", "b"]},
            "either": {"oneOf": [{"type": "string"}, {"type": "integer", "maximum": 5}]},
        },
        "required": ["format", "items"],
        "$defs": {
            "Item": {"type": "object", "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
                     "required": ["name"]},
            "Owner": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        },
    }
    original = json.loads(json.dumps(schema))
    strict = ensure_strict_schema(schema)
    assert schema == original                                                   # input is not mutated
    props = strict["properties"]
    assert strict["required"] == ["format", "default", "items", "owner", "kind", "either"]
    assert props["format"] == {"type": "string"} and props["default"] == {"type": ["integer", "null"]}
    assert props["items"] == {"type": "array", "items": {"$ref": "#/$defs/Item"}}
    assert props["owner"] == {"anyOf": [{"$ref": "#/$defs/Owner"}, {"type": "null"}]}
    assert props["kind"] == {"type": ["string", "null"], "enum": ["a", "b", None]}
    assert props["either"] == {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    item = strict["$defs"]["Item"]
    assert item["additionalProperties"] is False and item["required"] == ["name", "qty"]
    assert item["properties"]["qty"] == {"type": ["integer", "null"]}
    assert strict["title"] == "Proposal" and strict["additionalProperties"] is False


async def test_schema_name_is_sanitised(api):
    route = api.post(CHAT).mock(return_value=_ok())
    await _ask(_llm(), schema_name="CRM note: v2 (final)!")
    assert _body(route)["response_format"]["json_schema"]["name"] == "CRM_note__v2__final__"


async def test_temperature_rejection_retries_without_it_and_is_remembered(api):
    route = api.post(CHAT).mock(side_effect=[
        _error(400, "Unsupported value: 'temperature' does not support 0.0 with this model. Only the default (1) "
                    "value is supported.", param="temperature", code="unsupported_value"),
        _ok(), _ok()])
    llm = _llm()
    await _ask(llm)
    assert "temperature" in _body(route, 0) and "temperature" not in _body(route, 1)
    await _ask(llm)
    assert route.call_count == 3 and "temperature" not in _body(route, 2)       # learned: not sent again


async def test_concurrent_calls_all_recover_from_a_temperature_rejection(api):
    """Calls already in flight when the first rejection teaches the adapter must retry too, not fail."""
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)                             # let every call send its first request before any reply
        if "temperature" in json.loads(request.content):
            return _error(400, "Unsupported value: 'temperature' does not support 0.0 with this model. Only the "
                               "default (1) value is supported.", param="temperature", code="unsupported_value")
        return _ok()

    route = api.post(CHAT).mock(side_effect=handler)
    llm = _llm()
    results = await asyncio.gather(*[_ask(llm) for _ in range(6)])
    assert len(results) == 6 and all(r.data["summary"] == "ok" for r in results)
    assert route.call_count == 12                              # 6 rejected + 6 retried, nothing failed
    await _ask(llm)
    assert "temperature" not in _body(route, -1)               # and the lesson is remembered


async def test_max_completion_tokens_rejection_falls_back_to_max_tokens(api):
    route = api.post(CHAT).mock(side_effect=[
        _error(400, "Unrecognized request argument supplied: max_completion_tokens", code=None),
        _ok(), _ok()])
    llm = _llm()
    await _ask(llm, max_output_tokens=900)
    assert _body(route, 0)["max_completion_tokens"] == 900
    assert _body(route, 1)["max_tokens"] == 900 and "max_completion_tokens" not in _body(route, 1)
    await _ask(llm, max_output_tokens=500)
    assert _body(route, 2)["max_tokens"] == 500


async def test_pinned_token_limit_parameter_is_never_switched(api):
    route = api.post(CHAT).mock(side_effect=[
        _error(400, "Unsupported parameter: 'max_tokens' is not supported with this model. Use "
                    "'max_completion_tokens' instead.", param="max_tokens", code="unsupported_parameter"),
        _ok()])
    llm = _llm(token_limit_param="max_tokens")
    with pytest.raises(LlmRequestError):                      # a pinned parameter is never switched
        await _ask(llm)
    assert route.call_count == 1


async def test_reasoning_effort_is_sent_and_dropped_when_unsupported(api):
    route = api.post(CHAT).mock(side_effect=[
        _error(400, "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
               param="reasoning_effort", code="unsupported_parameter"),
        _ok()])
    await _ask(_llm(reasoning_effort="low"))
    assert _body(route, 0)["reasoning_effort"] == "low" and "reasoning_effort" not in _body(route, 1)


async def test_throttling_is_retried_with_retry_after_then_succeeds(api):
    sleeps = Sleeps()
    route = api.post(CHAT).mock(side_effect=[
        _error(429, "Rate limit reached", code="429", headers={"retry-after": "4"}),
        httpx.Response(503, json={"error": {"message": "busy"}}),
        _ok()])
    result = await _ask(_llm(sleep=sleeps, backoff_base=1.0))
    assert route.call_count == 3 and result.data["summary"] == "ok"
    assert sleeps.delays == [4.0, 2.0]                          # Retry-After first, then base * 2**1


async def test_connection_errors_are_retried(api):
    route = api.post(CHAT).mock(side_effect=[httpx.ConnectError("down"), _ok()])
    sleeps = Sleeps()
    await _ask(_llm(sleep=sleeps))
    assert route.call_count == 2 and sleeps.delays == [1.0]


async def test_persistent_throttling_raises_a_request_error(api):
    route = api.post(CHAT).mock(return_value=_error(429, "Rate limit reached", code="429"))
    with pytest.raises(LlmRequestError, match="HTTP 429") as info:
        await _ask(_llm(max_retries=2))
    assert route.call_count == 3 and info.value.status_code == 429 and info.value.retryable


async def test_a_tiny_retry_after_under_a_quota_ceiling_still_backs_off_for_about_a_minute(api):
    # Azure OpenAI answers a TPM-throttled call with ~200 ms; the default budget must outlast a saturated quota window
    sleeps = Sleeps()
    route = api.post(CHAT).mock(return_value=_error(429, "Rate limit reached", code="429", headers={"retry-after-ms": "200"}))
    with pytest.raises(LlmRequestError, match="HTTP 429"):
        await _ask(_llm(sleep=sleeps, backoff_base=1.0, backoff_cap=30.0))
    assert route.call_count == 7 and sleeps.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


async def test_excessive_retry_after_fails_fast(api):
    route = api.post(CHAT).mock(return_value=_error(429, "slow", headers={"retry-after": "3600"}))
    with pytest.raises(LlmRequestError):
        await _ask(_llm())
    assert route.call_count == 1


@pytest.mark.parametrize("status,needle", [(401, "Cognitive Services OpenAI User"), (403, "access denied"),
                                           (404, "VOICECRM_FOUNDRY_DEPLOYMENT")])
async def test_auth_and_deployment_errors_name_the_fix(api, status, needle):
    route = api.post(CHAT).mock(return_value=_error(status, "nope", code="x"))
    with pytest.raises(LlmRequestError, match=needle) as info:
        await _ask(_llm())
    assert route.call_count == 1 and info.value.status_code == status and not info.value.retryable


async def test_invalid_schema_is_reported_not_retried(api):
    route = api.post(CHAT).mock(return_value=_error(
        400, "Invalid schema for response_format 'crm_note': 'additionalProperties' is required",
        param="response_format", code="invalid_json_schema"))
    with pytest.raises(LlmRequestError, match="Invalid schema for response_format"):
        await _ask(_llm())
    assert route.call_count == 1


async def test_invalid_json_gets_one_repair_retry_and_usage_is_summed(api):
    route = api.post(CHAT).mock(side_effect=[
        httpx.Response(200, json=_completion('{"summary": "cut off', usage=(100, 40), cached=0)),
        httpx.Response(200, json=_completion('{"summary": "fixed", "tasks": []}', usage=(150, 25), cached=64)),
    ])
    result = await _ask(_llm())
    assert result.data == {"summary": "fixed", "tasks": []}
    assert (result.usage.tokens_in, result.usage.tokens_out, result.usage.cached_tokens) == (250, 65, 64)
    repair = _body(route, 1)["messages"]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == '{"summary": "cut off'
    assert "invalid JSON" in repair[3]["content"] and "ONLY one valid JSON object" in repair[3]["content"]


async def test_missing_required_property_triggers_the_repair_retry(api):
    route = api.post(CHAT).mock(side_effect=[_ok({"summary": "no tasks key"}), _ok({"summary": "s", "tasks": ["t"]})])
    result = await _ask(_llm())
    assert route.call_count == 2 and result.data["tasks"] == ["t"]
    assert "missing required properties: tasks" in _body(route, 1)["messages"][-1]["content"]


async def test_markdown_fenced_json_is_accepted_without_a_repair(api):
    route = api.post(CHAT).mock(return_value=httpx.Response(
        200, json=_completion('```json\n{"summary": "fenced", "tasks": []}\n```')))
    assert (await _ask(_llm())).data["summary"] == "fenced" and route.call_count == 1


async def test_two_unusable_replies_raise_llm_output_error_without_leaking_content(api):
    route = api.post(CHAT).mock(return_value=httpx.Response(200, json=_completion("this is NOT-JSON-SECRET")))
    with pytest.raises(LlmOutputError, match="after one repair retry") as info:
        await _ask(_llm())
    assert route.call_count == 2 and info.value.attempts == 2
    assert "NOT-JSON-SECRET" not in str(info.value)


async def test_truncated_output_error_mentions_the_token_limit(api):
    api.post(CHAT).mock(return_value=httpx.Response(200, json=_completion('{"summary": "abc', finish="length")))
    with pytest.raises(LlmOutputError, match="max_output_tokens=321") as info:
        await _ask(_llm(), max_output_tokens=321)
    assert info.value.finish_reason == "length"


async def test_refusals_and_content_filter_are_not_repaired(api):
    route = api.post(CHAT).mock(return_value=httpx.Response(200, json=_completion(None, refusal="I can't help")))
    with pytest.raises(LlmOutputError, match="refused"):
        await _ask(_llm())
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(200, json=_completion("", finish="content_filter")))
    with pytest.raises(LlmOutputError, match="content filter"):
        await _ask(_llm())
    assert route.call_count == 2


async def test_non_object_json_is_unusable(api):
    route = api.post(CHAT).mock(side_effect=[httpx.Response(200, json=_completion("[1, 2, 3]")), _ok()])
    await _ask(_llm())
    assert "not an object" in _body(route, 1)["messages"][-1]["content"]


async def test_no_prompt_or_output_text_is_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.post(CHAT).mock(side_effect=[
        httpx.Response(200, json=_completion('{"summary": "LEAK-OUT-JANE-4111 broken')),
        httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow"}}),
        _ok({"summary": "LEAK-OUT-JANE-4111", "tasks": []}),
    ])
    await _ask(_llm(), system="LEAK-SYSTEM-PROMPT", user="LEAK-USER-TRANSCRIPT-4111111111111111")
    assert "LEAK" not in caplog.text and "4111" not in caplog.text
    assert "foundry call ok" in caplog.text


async def test_owned_client_is_closed_but_injected_clients_are_not():
    http = httpx.AsyncClient(verify=False)
    injected = _llm(http_client=http)
    await injected.aclose()
    assert not http.is_closed
    await http.aclose()
    owned = FoundryLlm(endpoint=ENDPOINT, deployment="d", key="k", auth="key")
    await owned.aclose()


def test_configuration_errors_are_clear():
    with pytest.raises(ValueError, match="endpoint and the model deployment"):
        FoundryLlm(endpoint="", deployment="d", key="k", auth="key")
    with pytest.raises(ValueError, match="requires an API key"):
        FoundryLlm(endpoint=ENDPOINT, deployment="d", auth="key", key=None)

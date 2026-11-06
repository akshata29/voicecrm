"""FakeLlm: canned queue / callable responses, usage estimation and call recording."""
from __future__ import annotations

import pytest

from voicecrm.adapters.llm.errors import LlmOutputError
from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.ports.llm import LlmPort

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}


async def _ask(llm: FakeLlm, user: str = "hello", **kw):
    return await llm.generate_json(system="be brief", user=user, json_schema=SCHEMA, schema_name="note", **kw)


def test_implements_the_port():
    llm = FakeLlm()
    assert isinstance(llm, LlmPort) and llm.name == "fake" and llm.model == "fake-llm"


async def test_queue_is_consumed_in_order_and_calls_are_recorded():
    llm = FakeLlm([{"summary": "one"}, {"summary": "two"}], model="fake-gpt")
    first = await _ask(llm, "u1", max_output_tokens=123, temperature=0.3)
    second = await _ask(llm, "u2")
    assert (first.data, second.data) == ({"summary": "one"}, {"summary": "two"})
    assert first.usage.model == "fake-gpt"
    assert [c.user for c in llm.calls] == ["u1", "u2"]
    call = llm.calls[0]
    assert (call.system, call.schema_name, call.max_output_tokens, call.temperature) == ("be brief", "note", 123, 0.3)
    assert call.json_schema == SCHEMA and llm.last_call is llm.calls[-1]


async def test_exhausted_queue_raises_unless_repeat_last():
    llm = FakeLlm([{"summary": "only"}])
    await _ask(llm)
    with pytest.raises(LlmOutputError, match="no canned response left for call #2"):
        await _ask(llm)
    repeating = FakeLlm([{"summary": "again"}], repeat_last=True)
    assert [(await _ask(repeating)).data["summary"] for _ in range(3)] == ["again"] * 3


async def test_enqueue_and_single_dict_and_exception_entries():
    llm = FakeLlm({"summary": "solo"})
    llm.enqueue(RuntimeError("boom"), {"summary": "after"})
    assert (await _ask(llm)).data == {"summary": "solo"}
    with pytest.raises(RuntimeError, match="boom"):
        await _ask(llm)
    assert (await _ask(llm)).data == {"summary": "after"}
    assert len(llm.calls) == 3                                         # failed calls are recorded too


async def test_callable_responder_sync_and_async():
    sync = FakeLlm(lambda call: {"summary": call.user.upper()})
    assert (await _ask(sync, "abc")).data == {"summary": "ABC"}

    async def responder(call):
        return {"summary": f"{call.schema_name}:{call.max_output_tokens}"}

    assert (await _ask(FakeLlm(responder), max_output_tokens=7)).data == {"summary": "note:7"}
    failing = FakeLlm(lambda call: LlmOutputError("model gave up"))
    with pytest.raises(LlmOutputError, match="model gave up"):
        await _ask(failing)


async def test_usage_is_estimated_from_text_size():
    llm = FakeLlm([{"summary": "x" * 400}], chars_per_token=4, latency_ms=12)
    usage = (await _ask(llm, "y" * 800)).usage
    assert usage.tokens_in >= 200 and usage.tokens_out >= 100          # 800 user chars and ~400 output chars
    assert usage.latency_ms == 12 and usage.cached_tokens is None
    smaller = (await _ask(FakeLlm([{"summary": "x"}]), "y")).usage
    assert smaller.tokens_in < usage.tokens_in and smaller.tokens_out < usage.tokens_out


async def test_results_are_independent_copies():
    canned = {"summary": "keep", "items": [1, 2]}
    llm = FakeLlm([canned], repeat_last=True)
    first = await _ask(llm)
    first.data["items"].append(3)
    assert canned["items"] == [1, 2] and (await _ask(llm)).data["items"] == [1, 2]


async def test_non_dict_canned_response_is_rejected():
    with pytest.raises(LlmOutputError, match="must be a dict"):
        await _ask(FakeLlm(lambda call: ["not", "a", "dict"]))

"""The hybrid / llm engines through the real pipeline, with a deterministic stand-in for the model (no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from voicecrm.adapters.crm.local_sim import LocalCrm
from voicecrm.adapters.llm.errors import LlmOutputError
from voicecrm.adapters.llm.fake import FakeLlm, LlmCall
from voicecrm.adapters.pii.local import LocalPiiDetector
from voicecrm.adapters.transcription.mock import MockTranscriber
from voicecrm.adapters.vault.local_fs import LocalVault
from voicecrm.config import Settings
from voicecrm.container import _assemble, _load_bundle
from voicecrm.domain.enums import ConversationStatus
from voicecrm.pipeline.fixtures import ingest_from_scripted
from voicecrm.pipeline.policy_store import PolicyStore
from voicecrm.pipeline.service import PipelineService
from voicecrm.store.sqlite_store import SqliteStateStore

FIXTURE = "pb-teams-equity-class-c"


def _model(call: LlmCall) -> dict:
    """What a well-behaved model returns for the prompt payload: confirms stated classes, reuses the suggestions."""
    payload = json.loads(call.user)
    sections = [{"key": s["key"], "text": "Model-written overview." if s["kind"] == "text" else None,
                 "items": ["Model-written item."] if s["kind"] == "list" else []} for s in payload["template"]["sections"]]
    products = [{
        "mention_id": m["mention_id"], "mention_type": "interest", "confidence": 0.9,
        "chosen_product_id": next((c["product_id"] for c in m["candidates"] if c["share_class"] == m["share_class_stated"]), None),
    } for m in payload["product_mentions"]]
    suggestions = payload.get("suggestions", {})
    topics = [{"topic_id": t, "confidence": 0.8, "segment_ids": [payload["transcript"][0]["id"]]} for t in suggestions.get("topics", [])]
    tasks = [{"title": t["title"], "description": None, "owner_role": "seller", "due_date": t["due_date"], "due_text": t["due_text"],
              "segment_ids": t["segment_ids"], "confidence": 0.9} for t in suggestions.get("tasks", [])]
    return {"sections": sections, "products": products, "topics": topics, "tasks": tasks, "flags": [], "overall_confidence": 0.9}


def _service(tmp_path, llm: FakeLlm) -> SimpleNamespace:
    settings = Settings(_env_file=None, state_dir=tmp_path)
    data = _load_bundle(str(settings.data_dir))
    policies = PolicyStore.from_data_dir(settings.data_dir, None)
    store = SqliteStateStore(":memory:")
    deps = _assemble(settings=settings, data=data, policies=policies, store=store, crm=LocalCrm(":memory:", data.crm_seed),
                     vault=LocalVault(tmp_path / "vault"), transcription=MockTranscriber(list(data.scripted_calls.values()),
                                                                                         data_dir=settings.data_dir),
                     pii=LocalPiiDetector(), llm=llm, default_engine="hybrid")
    return SimpleNamespace(service=PipelineService(deps), store=store, call=data.scripted_calls[FIXTURE])


async def test_hybrid_engine_uses_the_model_and_records_token_usage(tmp_path):
    llm = FakeLlm(_model, model="fake-gpt")
    h = _service(tmp_path, llm)
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, nonce="llm"))
    assert rec.status == ConversationStatus.needs_review and rec.engine == "hybrid" and rec.proposal is not None
    result = rec.proposal.result
    assert result.engine.value == "hybrid" and result.model == "fake-gpt"
    assert any("Model-written overview." in (s.text or "") for s in result.sections)
    assert [m.chosen_product_id for m in result.products] == ["SYN-000002"]            # catalogue-grounded, class C only
    assert len(result.tasks) == 2 and all(t.due_date for t in result.tasks)
    usage = [u for u in h.store.list_usage(rec.meta.conversation_id) if u.service == "llm"]
    assert usage and usage[0].provider == "fake" and usage[0].tokens_in and usage[0].tokens_out
    assert len(llm.calls) == 1


async def test_prompt_contains_no_text_from_omitted_segments(tmp_path):
    llm = FakeLlm(_model)
    h = _service(tmp_path, llm)
    privacy = _load_bundle(str(Settings(_env_file=None).data_dir)).scripted_calls["retail-teams-relationship-privacy"]
    await h.service.run_to_completion(ingest_from_scripted(privacy, nonce="p"))
    prompt = llm.last_call.user.lower()
    assert privacy.gold.must_not_leak
    for secret in privacy.gold.must_not_leak:
        assert secret.lower() not in prompt


async def test_model_failure_falls_back_to_the_rules_engine_and_is_flagged(tmp_path):
    h = _service(tmp_path, FakeLlm([LlmOutputError("boom")]))
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, nonce="fail"))
    assert rec.status == ConversationStatus.needs_review and rec.engine == "rules"
    assert "llm_fallback" in rec.proposal.result.flags
    assert [m.chosen_product_id for m in rec.proposal.result.products] == ["SYN-000002"]


async def test_unavailable_engine_falls_back_with_a_flag(tmp_path):
    h = _service(tmp_path, FakeLlm(_model))
    h.service.deps.extractors.pop("llm")
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, engine="llm", nonce="na"))
    assert rec.engine == "rules" and "engine_unavailable:llm" in rec.proposal.result.flags


async def test_privacy_filter_usage_is_the_billed_text_records_not_the_segment_count(tmp_path):
    h = _service(tmp_path, FakeLlm(_model))
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, nonce="units"))
    (usage,) = [u for u in h.store.list_usage(rec.meta.conversation_id) if u.service == "language_pii"]
    assert 0 < usage.units < len(h.call.turns)                    # packed segments bill by characters, not one record each

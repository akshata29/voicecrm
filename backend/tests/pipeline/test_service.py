"""Orchestrator tests: real vault / CRM simulator / state store / PII rules / mock transcriber / source parsers,
with fake engines (see fakes.py), so they pass independently of the real extraction engines."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from voicecrm.adapters.crm.local_sim import LocalCrm
from voicecrm.adapters.pii.local import LocalPiiDetector
from voicecrm.adapters.transcription.mock import MockTranscriber
from voicecrm.adapters.vault.local_fs import LocalVault
from voicecrm.config import Settings
from voicecrm.data_loader import load_pricing, load_templates
from voicecrm.domain.catalogue import Catalogue, Product
from voicecrm.domain.conversation import ConversationMeta, Participant, Segment
from voicecrm.domain.crm import CrmSeed, SeedAccount, SeedContact, SeedOpportunity, SeedUser
from voicecrm.domain.enums import Channel, ConversationStatus, SpeakerRole, Stage, StageStatus, TranscriptOrigin
from voicecrm.domain.fixtures import GoldLabels, ScriptedCall, ScriptedTurn
from voicecrm.domain.pipeline import ApproveRequest, TaskEdit
from voicecrm.domain.taxonomy import Taxonomy, Topic
from voicecrm.pipeline.fixtures import ingest_from_scripted
from voicecrm.pipeline.models import IngestInput, PipelineDeps
from voicecrm.pipeline.policy_store import PolicyStore
from voicecrm.pipeline.service import PipelineError, PipelineService
from voicecrm.store.sqlite_store import SqliteStateStore

from .fakes import (
    FakeCost, FakeExtractor, FakeNotes, FakeRenderer, FakeResolver, FakeRetention, FakeValidator, fake_select_template,
)

UTC = timezone.utc


def _catalogue() -> Catalogue:
    def prod(pid: str, cls: str) -> Product:
        return Product(product_id=pid, family_id="F1", fund_name="Contoso Equity Fund", share_class=cls,
                       display_name=f"Contoso Equity Fund Class {cls}", asset_class="Equity", strategy="Core", region="US",
                       business_lines=["professional_buyer", "retail"])
    return Catalogue(version="t", products=[prod("P-C", "C"), prod("P-A", "A")], share_classes={"C": ["class c"], "A": ["class a"]})


def _meta(market: str = "US", source: Channel = Channel.teams, interaction: str = "src-1",
          business_line: str = "professional_buyer") -> ConversationMeta:
    return ConversationMeta(
        source=source, source_interaction_id=interaction, title="Equity call", started_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
        seller=Participant(id="seller", display_name="Sam Seller", email="sam@example.com", role=SpeakerRole.seller),
        participants=[Participant(id="cust1", display_name="Priya Nair", email="priya@northwind.example",
                                  org="Northwind Pension Fund", role=SpeakerRole.customer)],
        customer_org="Northwind Pension Fund", market=market, business_line=business_line, language_hint="en-US")


def _segments() -> list[Segment]:
    lines = [("Sam Seller", "Hi Priya, thanks for joining."),
             ("Priya Nair", "We are interested in the Contoso Equity Fund."),
             ("Sam Seller", "I'll send the fee comparison by Friday."),
             ("Priya Nair", "My card is 4111 1111 1111 1111."),
             ("Priya Nair", "Also my wife has cancer, so timing matters.")]
    return [Segment(id=i, speaker_id=who, start_ms=i * 4000, end_ms=i * 4000 + 3500, text=t, locale="en-US")
            for i, (who, t) in enumerate(lines)]


def _transcript_ingest(market: str = "US", interaction: str = "src-1", **kw) -> IngestInput:
    return IngestInput(meta=_meta(market, interaction=interaction), kind="transcript", segments=_segments(),
                       speakers=["Sam Seller", "Priya Nair"], transcript_origin=TranscriptOrigin.teams_platform,
                       language="en-US", **kw)


def _scripted_call() -> ScriptedCall:
    seller = Participant(id="seller", display_name="Sam Seller", email="sam@example.com", role=SpeakerRole.seller)
    cust = Participant(id="cust1", display_name="Priya Nair", email="priya@northwind.example", org="Northwind Pension Fund",
                       role=SpeakerRole.customer)
    turns = [ScriptedTurn(speaker="seller", text="Hi Priya, thanks for joining."),
             ScriptedTurn(speaker="customer", text="We are interested in the Contoso Equity Fund."),
             ScriptedTurn(speaker="seller", text="I'll send the fee comparison by Friday.")]
    return ScriptedCall(fixture_id="t-call", title="Equity call", description="d", channel=Channel.zoom, seller=seller,
                        customers=[cust], customer_org="Northwind Pension Fund", turns=turns,
                        gold=GoldLabels(template_id="pb_institutional"))


def make(tmp_path, *, extractor: FakeExtractor | None = None) -> SimpleNamespace:
    seed = CrmSeed(
        users=[SeedUser(id="u1", name="Sam Seller", email="sam@example.com")],
        accounts=[SeedAccount(id="acc1", name="Northwind Pension Fund", domain="northwind.example")],
        contacts=[SeedContact(id="con1", name="Priya Nair", email="priya@northwind.example", account_id="acc1")],
        opportunities=[SeedOpportunity(id="opp1", name="Equity mandate", account_id="acc1", contact_id="con1")])
    store, vault, crm = SqliteStateStore(":memory:"), LocalVault(tmp_path / "vault"), LocalCrm(":memory:", seed)
    policies = PolicyStore.from_data_dir(None, None)
    ext = extractor or FakeExtractor()
    deps = PipelineDeps(
        settings=Settings(_env_file=None, state_dir=tmp_path), store=store, transcription=MockTranscriber([_scripted_call()]),
        pii=LocalPiiDetector(), vault=vault, crm=crm, resolver=FakeResolver(), notes_engine=FakeNotes(),
        extractors={"rules": ext}, validator=FakeValidator(), renderer=FakeRenderer(), retention_engine=FakeRetention(),
        cost_engine=FakeCost(), catalogue=_catalogue(),
        taxonomy=Taxonomy(version="t", topics=[Topic(topic_id="fees_expenses", label="Fees and expenses"),
                                               Topic(topic_id="share_class_selection", label="Share class selection")]),
        templates=load_templates(), pricing=load_pricing(), policy_provider=lambda: policies.bundle,
        select_template=fake_select_template)
    return SimpleNamespace(service=PipelineService(deps), store=store, vault=vault, crm=crm, policies=policies, ext=ext, deps=deps)


async def test_platform_transcript_flows_to_review_with_events_usage_and_working_copy(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    assert rec.status == ConversationStatus.needs_review and rec.proposal is not None
    events = h.store.list_events(rec.meta.conversation_id)
    done = {e.stage: e.status for e in events if e.status != StageStatus.started}
    assert done[Stage.transcribe] == StageStatus.skipped                       # platform transcript: no transcription fee
    assert [Stage.ingest, Stage.transcribe, Stage.normalize, Stage.policy, Stage.extract, Stage.resolve, Stage.validate] == [
        s for s in dict.fromkeys(e.stage for e in events) if s != Stage.review]
    usage = h.store.list_usage(rec.meta.conversation_id)
    assert any(u.service == "speech" and u.provider == "platform" and u.audio_seconds == 0 for u in usage)
    assert any(u.service == "llm" and u.provider == "projected" for u in usage)       # rules engine: projected LLM tokens
    items = await h.vault.list_items(rec.meta.conversation_id)
    assert [i.kind for i in items] == ["working"] and items[0].redacted
    assert "product_needs_pick" in rec.proposal.review.reasons and not rec.proposal.review.auto_approve_eligible
    assert rec.proposal.crm_match.account.id == "acc1" and rec.proposal.crm_match.opportunity.id == "opp1"
    assert rec.language == "en-US"
    seller_ids = {s.speaker_id for s in (await h.vault.get_conversation(rec.meta.conversation_id, "working")).segments}
    assert seller_ids == {"seller", "cust1"}                                    # names mapped to participant ids


async def test_sensitive_content_is_redacted_before_it_reaches_the_proposal_or_working_copy(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    working = await h.vault.get_conversation(rec.meta.conversation_id, "working")
    text = " ".join(s.text for s in working.segments)
    assert "4111" not in text and "cancer" not in text and "[PAYMENT_CARD]" in text
    blob = rec.proposal.model_dump_json()
    assert "4111" not in blob and "cancer" not in blob.lower()
    kinds = {(a.kind, a.category) for a in rec.proposal.policy_actions}
    assert ("pii_redacted", "payment_card") in kinds and ("omitted_sensitive", "medical") in kinds


async def test_audio_path_uses_the_transcriber_meters_speech_and_deletes_audio_in_summary_only_market(tmp_path):
    h = make(tmp_path)
    ing = ingest_from_scripted(_scripted_call(), via="audio", nonce="a1")
    assert ing.kind == "audio" and ing.audio_bytes
    rec = await h.service.run_to_completion(ing)
    assert rec.status == ConversationStatus.needs_review and rec.transcript_origin == TranscriptOrigin.fixture
    assert any(u.service == "speech" and u.provider == "mock" and (u.audio_seconds or 0) > 0
               for u in h.store.list_usage(rec.meta.conversation_id))
    kinds = [i.kind for i in await h.vault.list_items(rec.meta.conversation_id)]
    assert "audio" not in kinds and "working" in kinds                          # US: audio_retention_days == 0


async def test_one_click_approve_writes_crm_and_us_market_discards_the_transcript(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest("US"))
    done = await h.service.approve(rec.meta.conversation_id, ApproveRequest())
    cid = done.meta.conversation_id
    assert done.status == ConversationStatus.written and done.crm_result.ok
    assert {r.entity for r in done.crm_result.created} >= {"phonecall", "task", "insight"}
    assert done.retention.transcript_stored is False and done.retention.crm_contains_transcript is False
    assert await h.vault.list_items(cid) == []                                  # working copy deleted; nothing retained
    assert all(ev.quote == "" for m in done.proposal.result.products for ev in m.evidence)       # quotes stripped (US)
    review = done.proposal.review
    assert review.state == "approved" and review.clicks == 1 and review.seconds_to_review is not None
    assert len(await h.crm.list_records("phonecall")) == 1 and len(await h.crm.list_records("task")) == 1
    conv, source, note = await h.service.get_transcript(cid)
    assert conv is None and source is None and "discarded" in note


async def test_eu_market_retains_a_redacted_transcript_in_the_vault_with_a_pointer_and_expiry(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest("EU"))
    done = await h.service.approve(rec.meta.conversation_id, ApproveRequest())
    cid = done.meta.conversation_id
    assert done.retention.transcript_stored and done.retention.transcript_expires_at is not None
    items = await h.vault.list_items(cid)
    assert [i.kind for i in items] == ["transcript"] and items[0].redacted and items[0].expires_at is not None
    pointer = done.proposal.crm_preview.insight.transcript_pointer
    assert pointer and "4111" not in pointer and "\n" not in pointer
    conv, source, note = await h.service.get_transcript(cid)
    assert source == "vault" and "4111" not in " ".join(s.text for s in conv.segments)
    assert any("cancer" not in r["summary_markdown"].lower() for r in await h.crm.list_records("insight"))


async def test_duplicate_delivery_is_idempotent_and_reprocess_after_failure_bumps_the_version(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    first = await h.service.submit(_transcript_ingest(interaction="dup-1"))
    await h.service.drain()
    again = await h.service.submit(_transcript_ingest(interaction="dup-1"))
    assert again.duplicate_of == first.conversation_id
    assert len(h.store.list_conversations()) == 1
    await h.service.reject(first.conversation_id, "not a real call")
    retry = await h.service.run_to_completion(_transcript_ingest(interaction="dup-1"))
    assert retry.meta.conversation_id != first.conversation_id and retry.version == 2


async def test_picking_a_product_records_feedback_but_does_not_learn_an_alias_for_an_unstated_class(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    cid = rec.meta.conversation_id
    done = await h.service.approve(cid, ApproveRequest(product_choices={"m1": "P-C"}, clicks=2))
    mention = done.proposal.result.products[0]
    assert mention.chosen_product_id == "P-C" and mention.confidence == 1.0 and not mention.needs_review
    assert "reviewer_selected" in mention.reasons and done.status == ConversationStatus.written
    assert [f.kind for f in h.store.list_feedback()] == ["product_choice"] and h.store.list_aliases() == []
    assert done.proposal.review.clicks == 2
    assert "Contoso Equity Fund Class C" in done.proposal.note_markdown          # derived section refreshed from the pick


async def test_correcting_a_misheard_product_learns_an_alias_and_reloads_the_resolver(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    cid = rec.meta.conversation_id
    stored = h.store.get_conversation(cid)
    stored.proposal.result.products[0].reasons = ["no_candidate"]               # the system never saw a share class cue
    stored.proposal.result.products[0].surface = "Contoso Equity Fund see shares"
    h.store.save_conversation(stored)
    await h.service.approve(cid, ApproveRequest(product_choices={"m1": "P-C"}))
    aliases = h.store.list_aliases()
    assert [(a.surface, a.product_id) for a in aliases] == [("contoso equity fund see shares", "P-C")]
    assert [a.surface for a in h.deps.resolver.aliases] == ["contoso equity fund see shares"]


async def test_edits_that_introduce_sensitive_content_are_refused(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest())
    with pytest.raises(PipelineError) as err:
        await h.service.approve(rec.meta.conversation_id, ApproveRequest(section_edits={"overview": "Pay with 4111 1111 1111 1111"}))
    assert err.value.status == 422
    assert h.store.get_conversation(rec.meta.conversation_id).status == ConversationStatus.needs_review
    assert await h.crm.list_records("phonecall") == []


async def test_task_and_topic_edits_flow_into_the_crm_package(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest())
    req = ApproveRequest(topic_ids=["share_class_selection"],
                         task_edits=[TaskEdit(task_id="t1", title="Send fee comparison and class conversion terms")])
    done = await h.service.approve(rec.meta.conversation_id, req)
    assert [t.topic_id for t in done.proposal.result.topics] == ["share_class_selection"]
    tasks = await h.crm.list_records("task")
    assert tasks and "class conversion terms" in tasks[0]["subject"]
    assert {f.kind for f in h.store.list_feedback()} == {"topic_edit", "task_edit"}


async def test_auto_approve_policy_writes_without_a_human(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    h.policies.update_processing(h.policies.bundle.processing.model_copy(update={"auto_approve_enabled": True}))
    rec = await h.service.run_to_completion(_transcript_ingest())
    assert rec.status == ConversationStatus.written
    review = rec.proposal.review
    assert review.state == "auto_approved" and review.clicks == 0 and rec.proposal.crm_preview.insight.review_status == "auto_approved"


async def test_auto_approve_never_fires_when_a_product_needs_a_pick(tmp_path):
    h = make(tmp_path)
    h.policies.update_processing(h.policies.bundle.processing.model_copy(update={"auto_approve_enabled": True}))
    rec = await h.service.run_to_completion(_transcript_ingest())
    assert rec.status == ConversationStatus.needs_review and not rec.proposal.review.auto_approve_eligible


async def test_reprocess_switches_template_from_the_working_copy(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest())
    assert rec.template_id == "pb_institutional"
    again = await h.service.reprocess(rec.meta.conversation_id, "retail_advisor", None)
    assert again.status == ConversationStatus.needs_review and again.proposal.template_id == "retail_advisor"
    assert [s.key for s in again.proposal.result.sections][:2] == ["overview", "advisor_situation"]
    assert {a.kind for a in again.proposal.policy_actions} >= {"pii_redacted", "omitted_sensitive"}    # audit trail preserved
    assert h.ext.calls == 2


async def test_reject_clears_working_copy_and_blocks_further_approval(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    done = await h.service.reject(rec.meta.conversation_id, "wrong customer")
    assert done.status == ConversationStatus.rejected and done.proposal.review.state == "rejected"
    assert await h.vault.list_items(rec.meta.conversation_id) == []
    with pytest.raises(PipelineError):
        await h.service.approve(rec.meta.conversation_id, ApproveRequest())
    with pytest.raises(PipelineError) as gone:
        await h.service.reprocess(rec.meta.conversation_id, None, None)
    assert gone.value.status in (409, 410)


async def test_failures_are_recorded_without_echoing_transcript_text(tmp_path):
    from pydantic import BaseModel, ValidationError

    class Strict(BaseModel):
        n: int

    try:
        Strict(n="the customer said my card is 4111 1111 1111 1111")       # type: ignore[arg-type]
    except ValidationError as exc:
        boom = exc
    h = make(tmp_path, extractor=FakeExtractor(fail=boom))
    rec = await h.service.run_to_completion(_transcript_ingest())
    assert rec.status == ConversationStatus.failed
    assert "4111" not in (rec.error or "") and "ValidationError" in (rec.error or "")
    failed = [e for e in h.store.list_events(rec.meta.conversation_id) if e.status == StageStatus.failed]
    assert failed and "4111" not in str(failed[0].detail)


async def test_unavailable_engine_falls_back_to_rules_and_is_flagged(tmp_path):
    h = make(tmp_path, extractor=FakeExtractor(ambiguous=False))
    rec = await h.service.run_to_completion(_transcript_ingest(engine="llm"))
    assert rec.engine == "rules" and "engine_unavailable:llm" in rec.proposal.result.flags


async def test_retention_sweep_removes_expired_vault_items(tmp_path):
    h = make(tmp_path)
    rec = await h.service.run_to_completion(_transcript_ingest())
    cid = rec.meta.conversation_id
    conv = await h.vault.get_conversation(cid, "working")
    await h.vault.put_conversation(cid, conv, kind="transcript", redacted=True,
                                   expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    deleted = await h.service.sweep_retention()
    assert [i.kind for i in deleted] == ["transcript"]
    assert [i.kind for i in await h.vault.list_items(cid)] == ["working"]


async def test_start_hands_back_the_task_of_the_call_and_nothing_for_a_duplicate(tmp_path):
    h = make(tmp_path)
    result, task = await h.service.start(_transcript_ingest(interaction="start-1"))
    assert result.duplicate_of is None and task is not None
    assert (await task).status == ConversationStatus.needs_review
    again, no_task = await h.service.start(_transcript_ingest(interaction="start-1"))
    assert again.duplicate_of == result.conversation_id and no_task is None


async def test_the_batch_tag_of_an_ingest_is_kept_on_the_record(tmp_path):
    h = make(tmp_path)
    tagged = _transcript_ingest(interaction="tag-1")
    tagged.batch_id = "load-abc"
    rec = await h.service.run_to_completion(tagged)
    plain = await h.service.run_to_completion(_transcript_ingest(interaction="tag-2"))
    assert rec.batch_id == "load-abc" and plain.batch_id is None
    assert h.store.batch_conversation_ids() == [rec.meta.conversation_id]


class _Gauge(FakeExtractor):
    """Remembers the most calls that were ever extracting at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self.now = self.peak = 0

    async def extract(self, ctx):
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(0.05)
            return await super().extract(ctx)
        finally:
            self.now -= 1


async def test_set_concurrency_changes_how_many_calls_run_at_once(tmp_path):
    gauge = _Gauge()
    h = make(tmp_path, extractor=gauge)

    async def run(count: int, tag: str) -> None:
        started = [await h.service.start(_transcript_ingest(interaction=f"{tag}-{n}")) for n in range(count)]
        await asyncio.gather(*(task for _, task in started if task is not None))

    assert h.service.set_concurrency(2) == 2 and h.service.stats.max_concurrency == 2
    await run(6, "narrow")
    assert gauge.peak == 2
    gauge.peak = 0
    h.service.set_concurrency(6)
    await run(6, "wide")
    assert gauge.peak > 2
    assert h.service.set_concurrency(0) == 1                             # never below one call at a time

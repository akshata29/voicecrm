"""Pipeline orchestration: ingest -> transcribe -> normalise -> policy -> extract -> resolve -> validate -> review -> write -> retention.

The service depends only on ports and engine Protocols (see PipelineDeps), so every stage is swappable between
mock/local and live adapters. Transcript text exists in memory during processing, as a short-lived redacted
'working' copy in the vault while a proposal awaits review, and as a durable redacted copy only when the market
policy retains transcripts. It is never written to the CRM or the state store.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain.catalogue import LearnedAlias
from ..domain.conversation import ArtifactRefs, CanonicalConversation, Participant, utcnow
from ..domain.enums import Channel, ConversationStatus, Engine, SpeakerRole, Stage, StageStatus, TranscriptOrigin
from ..domain.extraction import Evidence, ProductCandidate, TopicTag
from ..domain.metering import UsageRecord
from ..domain.pipeline import ApproveRequest, ConversationRecord, Proposal, RetentionDecision, ReviewInfo, StageEvent
from ..domain.policy import PolicyBundle
from ..domain.templates import SummaryTemplate
from ..engines.contracts import REVIEWER_EVIDENCE, ExtractionContext, ExtractionOutput, NotesPolicyResult
from ..ports.state import FeedbackRecord
from ..ports.transcription import AudioInput, TranscriptionRequest, TranscriptionResult
from ..ports.vault import VaultItem
from .crm_package import build_crm_package
from .models import IngestInput, IngestResult, PipelineDeps
from .speakers import detect_language, map_speakers, short_language
from .stats import PipelineStats

logger = logging.getLogger(__name__)

WORKING_COPY_TTL = timedelta(hours=24)
_PLATFORM_ORIGIN = {
    "teams": TranscriptOrigin.teams_platform,
    "zoom": TranscriptOrigin.zoom_platform,
    "genesys": TranscriptOrigin.genesys_platform,
}


class PipelineError(Exception):
    """A request the pipeline cannot honour (maps to HTTP 404 / 409 / 422 in the API)."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def normalize_surface(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()


def _safe_error(exc: Exception) -> str:
    """Error text for logs/UI that can never echo transcript content (pydantic errors embed input values)."""
    if isinstance(exc, ValidationError):
        return f"ValidationError in {sorted({'.'.join(str(p) for p in e['loc']) for e in exc.errors()})[:6]}"
    return f"{type(exc).__name__}: {str(exc)[:240]}"


class _StageCtx:
    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}
        self.skipped: str | None = None

    def skip(self, reason: str) -> None:
        self.skipped = reason


class PipelineService:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps
        self.stats = PipelineStats(max_concurrency=deps.max_concurrency)
        self._sem = asyncio.Semaphore(deps.max_concurrency)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._products = deps.catalogue.by_id()
        self._topics = deps.taxonomy.by_id()
        self.reload_aliases()

    # ------------------------------------------------------------------ helpers
    def reload_aliases(self) -> None:
        self.deps.resolver.reload_aliases(self.deps.store.list_aliases())

    def _emit(self, cid: str, stage: Stage, status: StageStatus, *, duration_ms: int | None = None,
              detail: dict[str, Any] | None = None) -> None:
        self.deps.store.add_event(StageEvent(conversation_id=cid, stage=stage, status=status, duration_ms=duration_ms,
                                             detail=detail or {}))

    @asynccontextmanager
    async def _stage(self, cid: str, stage: Stage) -> AsyncIterator[_StageCtx]:
        ctx = _StageCtx()
        t0 = time.perf_counter()
        self._emit(cid, stage, StageStatus.started)
        try:
            yield ctx
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            self.stats.stage(stage.value, ms, ok=False)
            self._emit(cid, stage, StageStatus.failed, duration_ms=ms, detail={"error": _safe_error(exc)})
            raise
        detail = dict(ctx.detail)
        status = StageStatus.completed
        if ctx.skipped:
            status = StageStatus.skipped
            detail["reason"] = ctx.skipped
        ms = int((time.perf_counter() - t0) * 1000)
        if status == StageStatus.completed:
            self.stats.stage(stage.value, ms)
        self._emit(cid, stage, status, duration_ms=ms, detail=detail)

    def _save(self, rec: ConversationRecord) -> ConversationRecord:
        rec.updated_at = utcnow()
        self.deps.store.save_conversation(rec)
        return rec

    def _usage(self, cid: str, stage: Stage, **kw: Any) -> None:
        self.deps.store.add_usage(UsageRecord(conversation_id=cid, stage=stage, **kw))

    def _require(self, cid: str) -> ConversationRecord:
        rec = self.deps.store.get_conversation(cid)
        if not rec:
            raise PipelineError(f"conversation {cid} not found", status=404)
        return rec

    def _fail(self, cid: str, exc: Exception) -> None:
        logger.warning("pipeline failed for %s: %s", cid, _safe_error(exc))
        rec = self.deps.store.get_conversation(cid)
        if rec:
            rec.status = ConversationStatus.failed
            rec.error = _safe_error(exc)
            self._save(rec)

    # ------------------------------------------------------------------ ingest
    def create_record(self, ing: IngestInput) -> tuple[ConversationRecord, bool]:
        """Returns (record, is_duplicate). Re-delivery of the same source interaction is idempotent."""
        meta = ing.meta
        existing = self.deps.store.find_by_source(meta.source.value, meta.source_interaction_id)
        if existing and existing.status not in (ConversationStatus.failed, ConversationStatus.rejected):
            return existing, True
        if not meta.participants:
            meta.participants = [Participant(id="cust1", display_name="Customer", role=SpeakerRole.customer,
                                             org=meta.customer_org)]
        rec = ConversationRecord(
            meta=meta, status=ConversationStatus.received, fixture_id=ing.fixture_id, batch_id=ing.batch_id,
            engine=ing.engine or self.deps.default_engine, version=(existing.version + 1) if existing else 1,
        )
        self._save(rec)
        self.stats.submitted()
        return rec, False

    async def start(self, ing: IngestInput) -> tuple[IngestResult, asyncio.Task[ConversationRecord] | None]:
        """Like submit(), but also hands back the processing task so a caller can wait for this one call."""
        rec, dup = self.create_record(ing)
        if dup:
            return IngestResult(conversation_id=rec.meta.conversation_id, duplicate_of=rec.meta.conversation_id), None
        task = asyncio.create_task(self.process(rec.meta.conversation_id, ing))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return IngestResult(conversation_id=rec.meta.conversation_id), task

    async def submit(self, ing: IngestInput) -> IngestResult:
        result, _ = await self.start(ing)
        return result

    def set_concurrency(self, limit: int) -> int:
        """Change how many calls run at once. Calls already holding a permit finish under the old limit."""
        limit = max(1, int(limit))
        self._sem = asyncio.Semaphore(limit)
        self.stats.max_concurrency = limit
        return limit

    async def run_to_completion(self, ing: IngestInput) -> ConversationRecord:
        rec, dup = self.create_record(ing)
        if dup:
            return rec
        return await self.process(rec.meta.conversation_id, ing)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @property
    def in_flight(self) -> int:
        """Calls being processed right now: a demo reset has to wait for them."""
        return len(self._tasks)

    # ------------------------------------------------------------------ processing
    async def process(self, cid: str, ing: IngestInput) -> ConversationRecord:
        queued_at = time.perf_counter()
        async with self._sem:
            self.stats.started(time.perf_counter() - queued_at)
            try:
                rec = self._require(cid)
                policies = self.deps.policy_provider()
                market = policies.markets.get(rec.meta.market) or next(iter(policies.markets.values()))
                rec.status = ConversationStatus.processing
                self._save(rec)
                async with self._stage(cid, Stage.ingest) as st:
                    st.detail.update(channel=rec.meta.source.value, market=market.market,
                                     business_line=rec.meta.business_line, kind=ing.kind)
                    if ing.audio_bytes is not None:
                        days = market.audio_retention_days
                        expires = utcnow() + (timedelta(days=days) if days > 0 else timedelta(hours=1))
                        item = await self.deps.vault.put_audio(
                            cid, ing.audio_bytes, filename=ing.audio_filename or "audio.wav",
                            content_type=ing.audio_content_type or "audio/wav", expires_at=expires)
                        st.detail.update(audio_bytes=len(ing.audio_bytes), audio_uri=item.uri)
                conv = await self._transcribe_and_normalize(rec, ing, policies)
                pol = await self._apply_policy(rec, conv, policies)
                await self._extract_and_propose(rec, pol, ing.requested_template_id, ing.engine, policies)
                rec = self._require(cid)
                if (policies.processing.auto_approve_enabled and rec.proposal
                        and rec.proposal.review.auto_approve_eligible):
                    await self.approve(cid, ApproveRequest(clicks=0), auto=True)
            except Exception as exc:
                self._fail(cid, exc)
            final = self._require(cid)
            failed = final.status == ConversationStatus.failed
            self.stats.finished(ok=not failed, total_seconds=time.perf_counter() - queued_at,
                                error=final.error if failed else None,
                                cost_usd=self.deps.cost_engine.estimate(self.deps.store.list_usage(cid), self.deps.pricing).total)
            return final

    def _choose_source(self, ing: IngestInput, policies: PolicyBundle) -> str:
        prefs = policies.processing.transcription_source_preference.get(ing.meta.source.value, ["platform", "azure_speech"])
        have_platform = ing.kind == "transcript" and bool(ing.segments)
        have_audio = ing.audio_bytes is not None
        for pref in prefs:
            if pref == "platform" and have_platform:
                return "platform"
            if pref == "azure_speech" and have_audio:
                return "audio"
        if have_platform:
            return "platform"
        if have_audio:
            return "audio"
        raise PipelineError("ingest carries neither a transcript nor audio", status=422)

    async def _transcribe_and_normalize(self, rec: ConversationRecord, ing: IngestInput,
                                        policies: PolicyBundle) -> CanonicalConversation:
        cid = rec.meta.conversation_id
        market = policies.markets.get(rec.meta.market) or next(iter(policies.markets.values()))
        async with self._stage(cid, Stage.transcribe) as st:
            source = self._choose_source(ing, policies)
            if source == "platform":
                segments = list(ing.segments)
                origin = ing.transcript_origin or _PLATFORM_ORIGIN.get(rec.meta.source.value, TranscriptOrigin.manual)
                if ing.asr_noise:
                    from ..adapters.transcription.noise import apply_asr_noise

                    segments = apply_asr_noise(segments)
                audio_seconds = (segments[-1].end_ms / 1000.0) if segments else 0.0
                self._usage(cid, Stage.transcribe, service="speech", provider="platform", audio_seconds=0.0)
                st.skip("platform transcript used - no transcription fee")
                st.detail.update(source="platform", origin=origin.value, segments=len(segments))
            else:
                result = await self._transcribe_audio(rec, ing)
                segments, origin, audio_seconds = list(result.segments), result.origin, result.audio_seconds
                self._usage(cid, Stage.transcribe, service="speech", provider=result.provider,
                            audio_seconds=result.audio_seconds)
                if market.audio_retention_days == 0:
                    await self.deps.vault.delete(cid, ("audio",))
                st.detail.update(source="azure_speech" if origin == TranscriptOrigin.azure_speech else origin.value,
                                 provider=result.provider, audio_seconds=round(result.audio_seconds, 1),
                                 segments=len(segments), audio_deleted=market.audio_retention_days == 0)
        async with self._stage(cid, Stage.normalize) as st:
            segments, people = map_speakers(segments, rec.meta)
            language = detect_language(segments, rec.meta.language_hint or ing.language)
            rec.language, rec.transcript_origin, rec.audio_seconds = language, origin, audio_seconds
            if not rec.meta.duration_seconds:
                rec.meta.duration_seconds = audio_seconds or None
            self._save(rec)
            st.detail.update(language=language, speakers=[p.display_name for p in people], segments=len(segments))
            return CanonicalConversation(meta=rec.meta, segments=segments, language=language, transcript_origin=origin,
                                         artifacts=ArtifactRefs())

    async def _transcribe_audio(self, rec: ConversationRecord, ing: IngestInput) -> TranscriptionResult:
        assert ing.audio_bytes is not None
        suffix = Path(ing.audio_filename or "audio.wav").suffix or ".wav"
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="voicecrm-")
        os.close(fd)
        try:
            Path(tmp).write_bytes(ing.audio_bytes)
            hint = rec.meta.language_hint or ing.language
            request = TranscriptionRequest(
                audio=AudioInput(filename=ing.audio_filename or "audio.wav", content_type=ing.audio_content_type or "audio/wav",
                                 path=tmp, sha256=hashlib.sha256(ing.audio_bytes).hexdigest()),
                locales=[hint] if hint else ["en-US", "es-US"],
                phrase_list=self.deps.resolver.phrase_list(500),
                diarize=True, max_speakers=max(2, 1 + len(rec.meta.participants)),
                separate_channels=rec.meta.source == Channel.genesys,
                fixture_id=ing.fixture_id, asr_noise=ing.asr_noise,
            )
            return await self.deps.transcription.transcribe(request)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _apply_policy(self, rec: ConversationRecord, conv: CanonicalConversation,
                            policies: PolicyBundle) -> NotesPolicyResult:
        cid = rec.meta.conversation_id
        async with self._stage(cid, Stage.policy) as st:
            texts = [s.text for s in conv.segments]
            found = await self.deps.pii.detect_many(texts, language=short_language(conv.language))
            spans = {seg.id: sp for seg, sp in zip(conv.segments, found, strict=False)}
            billed = getattr(self.deps.pii, "text_records", None)           # packing makes this fewer than the segments
            self._usage(cid, Stage.policy, service="language_pii", provider=getattr(self.deps.pii, "name", "pii"),
                        units=float(billed(texts) if callable(billed) else len(texts)))
            pol = self.deps.notes_engine.apply(conv, notes=policies.notes, pii_spans=spans)
            await self.deps.vault.put_conversation(cid, pol.redacted_conversation, kind="working", redacted=True,
                                                   expires_at=utcnow() + WORKING_COPY_TTL)
            counts: dict[str, int] = {}
            for action in pol.actions:
                counts[action.kind] = counts.get(action.kind, 0) + 1
            st.detail.update(pii_spans=sum(len(v) for v in spans.values()), actions=counts,
                             relationship_facts=len(pol.relationship_facts))
        return pol

    async def _extract_and_propose(self, rec: ConversationRecord, pol: NotesPolicyResult, template_id: str | None,
                                   engine: str | None, policies: PolicyBundle,
                                   keep_actions: list | None = None) -> ConversationRecord:
        cid = rec.meta.conversation_id
        template = self.deps.select_template(self.deps.templates, rec.meta, policies.processing, requested=template_id)
        engine_name = engine or rec.engine or self.deps.default_engine
        ctx = ExtractionContext(
            conversation=pol.redacted_conversation, policy=pol, template=template, call_date=rec.meta.started_at.date(),
            language=short_language(rec.language or "en"), business_line=rec.meta.business_line,
            top_k_products=policies.processing.top_k_products,
            product_review_threshold=policies.processing.product_review_threshold,
        )
        async with self._stage(cid, Stage.extract) as st:
            extractor = self.deps.extractors.get(engine_name)
            fallback_flag = None
            if extractor is None:
                fallback_flag = f"engine_unavailable:{engine_name}"
                extractor, engine_name = self.deps.extractors["rules"], "rules"
            try:
                out: ExtractionOutput = await extractor.extract(ctx)
            except Exception as exc:
                if engine_name == "rules":
                    raise
                logger.warning("engine %s failed (%s); falling back to rules", engine_name, _safe_error(exc))
                fallback_flag, engine_name = "llm_fallback", "rules"
                out = await self.deps.extractors["rules"].extract(ctx)
            if fallback_flag:
                out.result.flags.append(fallback_flag)
            for u in out.usage:
                self.deps.store.add_usage(u.model_copy(update={"conversation_id": cid, "stage": Stage.extract}))
            self.stats.extracted(engine_name, sum(u.tokens_in or 0 for u in out.usage),
                                 sum(u.tokens_out or 0 for u in out.usage), fallback=bool(fallback_flag))
            if not out.usage:
                chars = sum(len(s.text) for s in pol.redacted_conversation.segments)
                self._usage(cid, Stage.extract, service="llm", provider="projected", model="default",
                            tokens_in=900 + chars // 4, tokens_out=450)
            rec.engine, rec.template_id = engine_name, template.template_id
            st.detail.update(engine=engine_name, template=template.template_id, products=len(out.result.products),
                             topics=len(out.result.topics), tasks=len(out.result.tasks),
                             llm_tokens=sum((u.tokens_in or 0) + (u.tokens_out or 0) for u in out.usage),
                             flags=out.result.flags)
        async with self._stage(cid, Stage.resolve) as st:
            match = await self.deps.crm.match(rec.meta)
            st.detail.update(account=match.account.name if match.account else None, method=match.method,
                             ambiguous=match.ambiguous, confidence=round(match.confidence, 2))
        async with self._stage(cid, Stage.validate) as st:
            result, report = self.deps.validator.validate(out.result, ctx, notes=policies.notes)
            st.detail.update(passed=report.passed, leaks=report.leak_count, unsupported_claims=report.unsupported_claims,
                             findings=len(report.findings))
        proposal = self._make_proposal(rec, template, result, report, match,
                                       keep_actions if keep_actions is not None else pol.actions, policies)
        rec.proposal, rec.status, rec.error = proposal, ConversationStatus.needs_review, None
        self._save(rec)
        self._emit(cid, Stage.review, StageStatus.started,
                   detail={"reasons": proposal.review.reasons, "auto_approve_eligible": proposal.review.auto_approve_eligible})
        return rec

    def _make_proposal(self, rec: ConversationRecord, template: SummaryTemplate, result, report, match, actions,
                       policies: PolicyBundle) -> Proposal:
        self._refresh_derived_sections(result, template)
        note = self.deps.renderer.render(result, template, rec.meta, catalogue=self.deps.catalogue)
        proc = policies.processing
        reasons: list[str] = []
        if any(m.needs_review or m.chosen_product_id is None for m in result.products):
            reasons.append("product_needs_pick")
        if match.ambiguous:
            reasons.append("crm_match_ambiguous")
        elif not match.account:
            reasons.append("crm_match_missing")
        if not report.passed:
            reasons.append("validation_failed")
        blocking = [r for r in reasons if proc.auto_approve_requires_crm_match or not r.startswith("crm_")]
        eligible = not blocking and result.overall_confidence >= proc.auto_approve_min_confidence
        proposal = Proposal(template_id=template.template_id, note_markdown=note, result=result, validation=report,
                            crm_match=match, review=ReviewInfo(reasons=reasons, auto_approve_eligible=eligible),
                            policy_actions=list(actions), ready_at=utcnow())
        proposal.crm_preview = build_crm_package(rec, proposal, template, transcript_pointer=self._pointer_preview(rec, policies))
        return proposal

    def _pointer_preview(self, rec: ConversationRecord, policies: PolicyBundle) -> str | None:
        market = policies.markets.get(rec.meta.market)
        return f"vault://transcripts/{rec.meta.conversation_id}" if market and market.transcript_retention == "on" else None

    def _refresh_derived_sections(self, result, template: SummaryTemplate) -> None:
        """Keep the sections that mirror products and tasks in sync after reviewer edits."""
        by_field = {s.crm_field: s.key for s in template.sections if s.crm_field}
        key = by_field.get("vcrm_productsdiscussed")
        section = next((s for s in result.sections if s.key == key), None) if key else None
        if section is not None:
            lines = []
            for m in result.products:
                product = self._products.get(m.chosen_product_id) if m.chosen_product_id else None
                name = product.display_name if product else f"{m.surface} (share class to be confirmed)"
                lines.append(f"{name} - {m.mention_type.value.replace('_', ' ')}")
            if lines or section.items:
                section.items = lines
        key = by_field.get("vcrm_nextsteps")
        section = next((s for s in result.sections if s.key == key), None) if key else None
        if section is not None and result.tasks:
            section.items = [f"{t.title}" + (f" (due {t.due_date.isoformat()})" if t.due_date else "") for t in result.tasks]

    # ------------------------------------------------------------------ review
    async def approve(self, cid: str, req: ApproveRequest, *, reviewer: str = "reviewer", auto: bool = False) -> ConversationRecord:
        rec = self._require(cid)
        if rec.status not in (ConversationStatus.needs_review, ConversationStatus.failed) or not rec.proposal:
            raise PipelineError(f"conversation is {rec.status.value}; nothing to approve")
        if req.template_id and req.template_id not in self.deps.templates:
            raise PipelineError(f"unknown template '{req.template_id}'", status=422)
        if req.template_id and req.template_id != rec.proposal.template_id:
            rec = await self.reprocess(cid, req.template_id, None)
        proposal = rec.proposal
        assert proposal is not None
        await self._apply_edits(rec, proposal, req)
        now = utcnow()
        proposal.review = proposal.review.model_copy(update={
            "state": "auto_approved" if auto else "approved", "reviewed_at": now,
            "reviewed_by": "auto-approve policy" if auto else reviewer, "clicks": 0 if auto else max(req.clicks, 1),
            "seconds_to_review": (now - proposal.ready_at).total_seconds() if proposal.ready_at else None,
        })
        rec.status = ConversationStatus.approved
        self._save(rec)
        self._emit(cid, Stage.review, StageStatus.completed,
                   duration_ms=int(proposal.review.seconds_to_review * 1000) if proposal.review.seconds_to_review is not None else None,
                   detail={"state": proposal.review.state, "clicks": proposal.review.clicks,
                           "seconds_to_review": proposal.review.seconds_to_review,
                           "edits": bool(req.product_choices or req.topic_ids is not None or req.task_edits
                                         or req.section_edits or req.drop_mentions)})
        return await self._finalize(rec, review_status="auto_approved" if auto else "approved")

    async def _apply_edits(self, rec: ConversationRecord, proposal: Proposal, req: ApproveRequest) -> None:
        result, cid = proposal.result, rec.meta.conversation_id
        edited = False
        if req.drop_mentions:
            result.products = [m for m in result.products if m.mention_id not in req.drop_mentions]
            edited = True
        for mention_id, product_id in req.product_choices.items():
            mention = next((m for m in result.products if m.mention_id == mention_id), None)
            product = self._products.get(product_id)
            if mention is None or product is None:
                raise PipelineError(f"cannot apply product choice {mention_id}->{product_id}", status=422)
            previous, learn = mention.chosen_product_id, "share_class_not_stated" not in mention.reasons
            mention.chosen_product_id, mention.confidence, mention.needs_review = product_id, 1.0, False
            mention.reasons = [*mention.reasons, "reviewer_selected"]
            if product_id not in {c.product_id for c in mention.candidates}:
                mention.candidates.insert(0, ProductCandidate(
                    product_id=product_id, display_name=product.display_name, fund_name=product.fund_name,
                    share_class=product.share_class, score=1.0, matched_on=["reviewer"]))
            self.deps.store.add_feedback(FeedbackRecord(conversation_id=cid, kind="product_choice", payload={
                "mention_id": mention_id, "product_id": product_id, "previous": previous, "learned": learn}))
            if learn and previous != product_id:
                self.deps.store.upsert_alias(LearnedAlias(surface=normalize_surface(mention.surface), product_id=product_id))
            edited = True
        if req.product_choices:
            self.reload_aliases()
        if req.topic_ids is not None:
            existing = {t.topic_id: t for t in result.topics}
            result.topics = [existing.get(t) or TopicTag(topic_id=t, label=self._topics[t].label, confidence=1.0,
                                                          evidence=[Evidence(segment_ids=[], quote=REVIEWER_EVIDENCE)])
                             for t in req.topic_ids if t in self._topics]
            self.deps.store.add_feedback(FeedbackRecord(conversation_id=cid, kind="topic_edit", payload={"topic_ids": req.topic_ids}))
            edited = True
        for edit in req.task_edits:
            task = next((t for t in result.tasks if t.task_id == edit.task_id), None)
            if task is None:
                continue
            if edit.drop:
                result.tasks = [t for t in result.tasks if t.task_id != edit.task_id]
            else:
                if edit.title:
                    task.title = edit.title
                if edit.due_date:
                    task.due_date, task.due_basis = edit.due_date, "stated"
            self.deps.store.add_feedback(FeedbackRecord(conversation_id=cid, kind="task_edit", payload=edit.model_dump(mode="json")))
            edited = True
        template = self.deps.templates[proposal.template_id]
        for key, text in req.section_edits.items():
            section = next((s for s in result.sections if s.key == key), None)
            if section is None:
                continue
            kind = next((s.kind for s in template.sections if s.key == key), "text")
            if kind == "list":
                section.items = [line.strip("-• ").strip() for line in text.splitlines() if line.strip()]
            else:
                section.text = text
            self.deps.store.add_feedback(FeedbackRecord(conversation_id=cid, kind="section_edit", payload={"section": key}))
            edited = True
        if not edited:
            return
        self._refresh_derived_sections(result, template)
        policies = self.deps.policy_provider()
        working = await self.deps.vault.get_conversation(cid, "working")
        if working is not None:
            ctx = ExtractionContext(
                conversation=working, policy=NotesPolicyResult(segments=[], redacted_conversation=working), template=template,
                call_date=rec.meta.started_at.date(), language=short_language(rec.language or "en"),
                business_line=rec.meta.business_line)
            cleaned, report = self.deps.validator.validate(result, ctx, notes=policies.notes)
            if report.leak_count:
                raise PipelineError("edited content contains sensitive information and cannot be sent to the CRM", status=422)
            proposal.result, proposal.validation = cleaned, report
        proposal.note_markdown = self.deps.renderer.render(proposal.result, template, rec.meta, catalogue=self.deps.catalogue)
        proposal.crm_preview = build_crm_package(rec, proposal, template, transcript_pointer=self._pointer_preview(rec, policies))

    async def reject(self, cid: str, reason: str | None = None) -> ConversationRecord:
        rec = self._require(cid)
        if rec.status not in (ConversationStatus.needs_review, ConversationStatus.failed):
            raise PipelineError(f"conversation is {rec.status.value}; nothing to reject")
        if rec.proposal:
            now = utcnow()
            rec.proposal.review = rec.proposal.review.model_copy(update={
                "state": "rejected", "reviewed_at": now, "reviewed_by": "reviewer", "clicks": 1,
                "seconds_to_review": (now - rec.proposal.ready_at).total_seconds() if rec.proposal.ready_at else None})
            self._strip_quotes(rec.proposal)
        rec.status = ConversationStatus.rejected
        self._save(rec)
        self.deps.store.add_feedback(FeedbackRecord(conversation_id=cid, kind="reject", payload={"reason": reason or ""}))
        await self.deps.vault.delete(cid, ("working", "audio"))
        self._emit(cid, Stage.review, StageStatus.completed, detail={"state": "rejected"})
        return rec

    async def reprocess(self, cid: str, template_id: str | None, engine: str | None) -> ConversationRecord:
        rec = self._require(cid)
        if rec.status not in (ConversationStatus.needs_review, ConversationStatus.failed):
            raise PipelineError(f"conversation is {rec.status.value}; it can only be reprocessed while pending review")
        if template_id and template_id not in self.deps.templates:
            raise PipelineError(f"unknown template '{template_id}'", status=422)
        working = await self.deps.vault.get_conversation(cid, "working")
        if working is None:
            raise PipelineError("the working copy has expired or the market policy discarded it; re-ingest the call", status=410)
        policies = self.deps.policy_provider()
        keep = list(rec.proposal.policy_actions) if rec.proposal else None
        pol = self.deps.notes_engine.apply(working, notes=policies.notes, pii_spans={})
        rec.status = ConversationStatus.processing
        self._save(rec)
        try:
            await self._extract_and_propose(rec, pol, template_id, engine, policies, keep_actions=keep)
        except Exception as exc:
            self._fail(cid, exc)
        return self._require(cid)

    # ------------------------------------------------------------------ write + retention
    async def _finalize(self, rec: ConversationRecord, *, review_status: str) -> ConversationRecord:
        cid, proposal = rec.meta.conversation_id, rec.proposal
        assert proposal is not None
        policies = self.deps.policy_provider()
        market = policies.markets.get(rec.meta.market) or next(iter(policies.markets.values()))
        template = self.deps.templates[proposal.template_id]
        decision: RetentionDecision = self.deps.retention_engine.decide(market, now=utcnow())
        try:
            pointer: str | None = None
            async with self._stage(cid, Stage.retention) as st:
                if decision.transcript_stored:
                    working = await self.deps.vault.get_conversation(cid, "working")
                    if working is not None:
                        item: VaultItem = await self.deps.vault.put_conversation(
                            cid, working, kind="transcript", redacted=True, expires_at=decision.transcript_expires_at)
                        pointer = item.uri
                st.detail.update(market=market.market, transcript_stored=decision.transcript_stored,
                                 transcript_expires_at=decision.transcript_expires_at.isoformat()
                                 if decision.transcript_expires_at else None, explanation=decision.explanation)
            async with self._stage(cid, Stage.write) as st:
                proposal.crm_preview = build_crm_package(rec, proposal, template, review_status=review_status,  # type: ignore[arg-type]
                                                         transcript_pointer=pointer)
                result = await self.deps.crm.write(proposal.crm_preview)
                rec.crm_result = result
                st.detail.update(ok=result.ok, duplicate=result.skipped_duplicate, created=len(result.created),
                                 degraded=result.degraded_fields, crm=getattr(self.deps.crm, "name", "crm"))
                if not result.ok:
                    raise PipelineError(f"CRM write failed: {result.error}")
            rec.retention = decision
            rec.status = ConversationStatus.written
            if not decision.transcript_stored:
                self._strip_quotes(proposal)
            await self.deps.vault.delete(cid, ("working",))
        except Exception as exc:
            rec.status, rec.error = ConversationStatus.failed, _safe_error(exc)
        return self._save(rec)

    @staticmethod
    def _strip_quotes(proposal: Proposal) -> None:
        """Markets that discard transcripts also drop the verbatim evidence quotes (segment ids are kept)."""
        result = proposal.result
        for group in (result.products, result.topics, result.tasks, result.relationship_facts):
            for item in group:
                for ev in item.evidence:
                    ev.quote = ""

    # ------------------------------------------------------------------ transcript + retention sweep
    async def get_transcript(self, cid: str) -> tuple[CanonicalConversation | None, str | None, str]:
        rec = self._require(cid)
        if rec.status in (ConversationStatus.needs_review, ConversationStatus.failed):
            working = await self.deps.vault.get_conversation(cid, "working")
            if working is not None:
                return working, "working", "Redacted working copy, kept only while the proposal awaits review."
        stored = await self.deps.vault.get_conversation(cid, "transcript")
        if stored is not None:
            exp = rec.retention.transcript_expires_at if rec.retention else None
            return stored, "vault", ("Redacted transcript retained in the vault per market policy"
                                     + (f" until {exp:%Y-%m-%d}." if exp else "."))
        if rec.retention:
            return None, None, rec.retention.explanation
        return None, None, "No transcript is available yet."

    async def sweep_retention(self) -> list[VaultItem]:
        return await self.deps.vault.sweep_expired(utcnow())

    def transcript_expected(self, rec: ConversationRecord) -> bool:
        return rec.status in (ConversationStatus.needs_review, ConversationStatus.failed) or bool(
            rec.retention and rec.retention.transcript_stored)

    def engine_names(self) -> list[str]:
        return [e.value for e in Engine if e.value in self.deps.extractors]

    def retention_preview(self, market: str) -> RetentionDecision:
        policies = self.deps.policy_provider()
        mp = policies.markets.get(market)
        if mp is None:
            raise PipelineError(f"unknown market {market}", status=404)
        return self.deps.retention_engine.decide(mp, now=utcnow())

    def now(self) -> datetime:
        return utcnow()

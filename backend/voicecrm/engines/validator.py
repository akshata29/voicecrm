"""Independent output validator: the last gate before a proposal reaches a reviewer or the CRM.

It never trusts the extractor. Every CRM-bound field is scanned again for sensitive data (card / bank / government id /
credentials / personal contact via the local PII detector, medical disclosures via the notes-policy lexicon), claims
are checked for transcript evidence, products are checked against the catalogue and topics against the taxonomy, and
the result is checked against the template contract. Leaks are scrubbed in the returned copy *and* reported, so the
reviewer sees that something had to be removed.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from ..adapters.pii.local import LocalPiiDetector
from ..domain.catalogue import Catalogue
from ..domain.enums import OwnerRole
from ..domain.extraction import (
    Evidence, ExtractionResult, NoteSection, TaskItem, TopicTag, ValidationFinding, ValidationReport,
)
from ..domain.policy import NotesPolicy
from ..domain.taxonomy import Taxonomy
from .contracts import REVIEWER_EVIDENCE, ExtractionContext
from .notes_policy import RulesNotesPolicyEngine
from .text import norm_text

_PLACEHOLDER = "[{}]"


class RulesValidator:
    """Implements engines.contracts.Validator."""

    def __init__(self, catalogue: Catalogue, taxonomy: Taxonomy, *, pii: LocalPiiDetector | None = None) -> None:
        self._products = catalogue.by_id()
        self._topics = taxonomy.by_id()
        self._pii = pii or LocalPiiDetector()

    # ------------------------------------------------------------------ entry point
    def validate(self, result: ExtractionResult, ctx: ExtractionContext, *,
                 notes: NotesPolicy) -> tuple[ExtractionResult, ValidationReport]:
        cleaned = result.model_copy(deep=True)
        findings: list[ValidationFinding] = []
        leaks = 0
        unsupported = 0
        omit = set(notes.omit_categories)

        def scrub(text: str, where: str) -> str:
            nonlocal leaks
            out, found = self._scrub(text, omit)
            for category in found:
                leaks += 1
                findings.append(ValidationFinding(severity="error", code=f"leak.{category}", where=where,
                                                  message=f"{category.replace('_', ' ')} found in a CRM-bound field and removed"))
            return out

        for section in cleaned.sections:
            if section.text:
                section.text = scrub(section.text, f"section:{section.key}")
            section.items = [scrub(i, f"section:{section.key}") for i in section.items]
        for task in cleaned.tasks:
            task.title = scrub(task.title, f"task:{task.task_id}")
            if task.description:
                task.description = scrub(task.description, f"task:{task.task_id}")
        for fact in cleaned.relationship_facts:
            fact.text = scrub(fact.text, "relationship_fact")
        for mention in cleaned.products:
            mention.surface = scrub(mention.surface, f"product:{mention.mention_id}")
        for ev in self._all_evidence(cleaned):
            ev.quote = scrub(ev.quote, "evidence")

        self._check_products(cleaned, findings)
        unsupported += self._check_evidence(cleaned, ctx, findings)
        self._check_topics(cleaned, findings)
        self._check_tasks(cleaned, ctx, findings)
        self._check_relationship(cleaned, ctx, findings)
        self._check_template(cleaned, ctx, findings)

        passed = not any(f.severity == "error" for f in findings)
        return cleaned, ValidationReport(passed=passed, findings=findings, leak_count=leaks, unsupported_claims=unsupported)

    # ------------------------------------------------------------------ scrubbing
    def _scrub(self, text: str, omit: set[str]) -> tuple[str, list[str]]:
        if not text:
            return text, []
        found: list[str] = []
        spans = [s for s in self._pii.detect(text) if s.category in omit]
        for span in sorted(spans, key=lambda s: s.start, reverse=True):
            text = text[:span.start] + _PLACEHOLDER.format(span.category.upper()) + text[span.end:]
            found.append(span.category)
        category = RulesNotesPolicyEngine._sensitive_category(text)
        if category and category in omit:
            found.append(category)
            text = _PLACEHOLDER.format(f"omitted: {category}")
        return text, found

    @staticmethod
    def _all_evidence(result: ExtractionResult) -> list[Evidence]:
        out: list[Evidence] = []
        for m in result.products:
            out.extend(m.evidence)
        for t in result.topics:
            out.extend(t.evidence)
        for t in result.tasks:
            out.extend(t.evidence)
        for f in result.relationship_facts:
            out.extend(f.evidence)
        return out

    # ------------------------------------------------------------------ catalogue and taxonomy
    def _check_products(self, result: ExtractionResult, findings: list[ValidationFinding]) -> None:
        for m in result.products:
            if m.chosen_product_id and m.chosen_product_id not in self._products:
                findings.append(ValidationFinding(severity="error", code="catalogue.unknown_product", where=f"product:{m.mention_id}",
                                                  message="the chosen product is not in the catalogue; it was cleared for review"))
                m.chosen_product_id, m.needs_review = None, True
                m.reasons = [*m.reasons, "unknown_product"]
            known = [c for c in m.candidates if c.product_id in self._products]
            if len(known) != len(m.candidates):
                findings.append(ValidationFinding(severity="warning", code="catalogue.unknown_candidate", where=f"product:{m.mention_id}",
                                                  message="candidates outside the catalogue were dropped"))
                m.candidates = known
            if m.chosen_product_id and m.share_class_stated:
                chosen = self._products[m.chosen_product_id]
                if chosen.share_class != m.share_class_stated:
                    findings.append(ValidationFinding(severity="error", code="catalogue.share_class_mismatch", where=f"product:{m.mention_id}",
                                                      message=f"the speaker said class {m.share_class_stated} but the product is class "
                                                              f"{chosen.share_class}; cleared for review"))
                    m.chosen_product_id, m.needs_review = None, True
                    m.reasons = [*m.reasons, "share_class_mismatch"]

    def _check_topics(self, result: ExtractionResult, findings: list[ValidationFinding]) -> None:
        kept: list[TopicTag] = []
        for t in result.topics:
            topic = self._topics.get(t.topic_id)
            if topic is None:
                findings.append(ValidationFinding(severity="error", code="taxonomy.unknown_topic", where=f"topic:{t.topic_id}",
                                                  message="topic is not in the taxonomy and was dropped"))
                continue
            t.label = topic.label
            kept.append(t)
        result.topics = kept

    # ------------------------------------------------------------------ evidence
    def _check_evidence(self, result: ExtractionResult, ctx: ExtractionContext, findings: list[ValidationFinding]) -> int:
        texts = {s.id: norm_text(s.text) for s in ctx.conversation.segments}
        unsupported = 0

        def supported(evidence: list[Evidence]) -> bool:
            for ev in evidence:
                if not ev.segment_ids and ev.quote == REVIEWER_EVIDENCE:
                    return True                                  # a reviewer added this by hand
                ids = [i for i in ev.segment_ids if i in texts]
                if not ids:
                    continue
                quote = norm_text(ev.quote)
                joined = " ".join(texts[i] for i in ids)
                if quote and (quote in joined or fuzz.partial_ratio(quote, joined) >= 85):
                    return True
            return False

        for m in result.products:
            if not supported(m.evidence):
                unsupported += 1
                m.needs_review = True
                m.reasons = [*m.reasons, "no_evidence"]
                findings.append(ValidationFinding(severity="warning", code="evidence.missing", where=f"product:{m.mention_id}",
                                                  message="no transcript evidence for this product; sent to review"))
        topics: list[TopicTag] = []
        for t in result.topics:
            if supported(t.evidence):
                topics.append(t)
            else:
                unsupported += 1
                findings.append(ValidationFinding(severity="warning", code="evidence.missing", where=f"topic:{t.topic_id}",
                                                  message="topic without transcript evidence dropped"))
        result.topics = topics
        tasks: list[TaskItem] = []
        for t in result.tasks:
            if supported(t.evidence):
                tasks.append(t)
            else:
                unsupported += 1
                findings.append(ValidationFinding(severity="warning", code="evidence.missing", where=f"task:{t.task_id}",
                                                  message="task without transcript evidence dropped"))
        result.tasks = tasks
        return unsupported

    # ------------------------------------------------------------------ tasks, relationship, template
    def _check_tasks(self, result: ExtractionResult, ctx: ExtractionContext, findings: list[ValidationFinding]) -> None:
        kept: list[TaskItem] = []
        for t in result.tasks:
            if t.owner_role == OwnerRole.customer:
                findings.append(ValidationFinding(severity="warning", code="task.customer_commitment", where=f"task:{t.task_id}",
                                                  message="customer commitments are context, not CRM tasks; dropped"))
                continue
            if t.due_date and (t.due_date < ctx.call_date or (t.due_date - ctx.call_date).days > 366):
                findings.append(ValidationFinding(severity="warning", code="task.due_out_of_range", where=f"task:{t.task_id}",
                                                  message="due date is before the call or more than a year out; cleared"))
                t.due_date, t.due_basis = None, "none"
            kept.append(t)
        for n, t in enumerate(kept, 1):
            t.task_id = f"t{n}"
        result.tasks = kept

    @staticmethod
    def _check_relationship(result: ExtractionResult, ctx: ExtractionContext, findings: list[ValidationFinding]) -> None:
        if result.relationship_facts and not ctx.template.include_relationship_context:
            findings.append(ValidationFinding(severity="warning", code="policy.relationship_not_allowed", where="relationship_facts",
                                              message="this template does not carry relationship context; facts removed"))
            result.relationship_facts = []

    @staticmethod
    def _check_template(result: ExtractionResult, ctx: ExtractionContext, findings: list[ValidationFinding]) -> None:
        present = {s.key: s for s in result.sections}
        ordered: list[NoteSection] = []
        for spec in ctx.template.sections:
            section = present.get(spec.key)
            if section is None:
                findings.append(ValidationFinding(severity="warning", code="template.missing_section", where=f"section:{spec.key}",
                                                  message="section missing from the model output; added empty"))
                section = NoteSection(key=spec.key, title=spec.title)
            if spec.kind == "list" and spec.max_items is not None and len(section.items) > spec.max_items:
                findings.append(ValidationFinding(severity="warning", code="template.too_many_items", where=f"section:{spec.key}",
                                                  message=f"truncated to {spec.max_items} items"))
                section.items = section.items[: spec.max_items]
            if spec.required and not (section.text or section.items):
                findings.append(ValidationFinding(severity="warning", code="template.empty_required_section", where=f"section:{spec.key}",
                                                  message="required section is empty"))
            ordered.append(section)
        result.sections = ordered

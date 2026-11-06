"""Rules-based (extractive) note builder: fills every section of a template from structured extraction results.

No language model is involved: the overview is assembled from call metadata, resolved products, topics and tasks, and the
context / concern lists are the most informative customer sentences (rewritten to third person). Spanish calls get a
structured English note (free-text sections need the LLM engine - a flag says so) because the rules engine does not
translate.
"""
from __future__ import annotations

import re
from datetime import date

from ..domain.conversation import Participant
from ..domain.enums import Channel, Direction, SpeakerRole
from ..domain.extraction import NoteSection, ProductMention, TaskItem, TopicTag
from ..domain.templates import SummaryTemplate, TemplateSection
from .contracts import ExtractionContext, PolicySegment
from .text import clip, contains_phrase, norm_words, sentence_spans

_CONTEXT_CUES = frozenset({
    "mandate", "mandates", "objective", "objectives", "goal", "goals", "looking", "need", "needs", "requirement", "requirements",
    "committee", "approve", "approval", "approved", "decision", "timeline", "deadline", "budget", "constraint", "constraints",
    "platform", "custodian", "allocation", "portfolio", "portfolios", "clients", "advisors", "assets", "aum", "rfp", "screening",
    "shortlist", "evaluating", "considering", "replace", "replacing", "search", "sleeve", "income", "retired", "retirement",
    "taxable", "diversify", "diversification", "model", "sleeves", "pension", "endowment", "participants", "default",
})
_CONCERN_CUES = frozenset({
    "concern", "concerned", "concerns", "worried", "worry", "question", "wondering", "unsure", "issue", "issues", "problem",
    "problems", "pushback", "expensive", "higher", "risk", "risks", "worrying", "afraid", "confused", "unclear",
})
_CONCERN_PHRASES = ("how does", "how do", "what about", "not sure", "why is", "why are", "is there", "are there", "what is", "what's",
                    "would that", "do you have")
_SKIP_START = re.compile(r"^\s*(?:hold on|thanks|thank you|good catch|got it|sorry|hi\b|hello|hey\b|morning|good morning|please|"
                         r"that'?s (?:helpful|great|right)|sounds good|absolutely|perfect|great\b|sure\b)", re.I)
_PRONOUNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bwe(?:'|\u2019)re\b", re.I), "they are"), (re.compile(r"\bwe(?:'|\u2019)ve\b", re.I), "they have"),
    (re.compile(r"\bwe(?:'|\u2019)ll\b", re.I), "they will"), (re.compile(r"\bwe(?:'|\u2019)d\b", re.I), "they would"),
    (re.compile(r"\bi(?:'|\u2019)m\b", re.I), "they are"), (re.compile(r"\bi(?:'|\u2019)ve\b", re.I), "they have"),
    (re.compile(r"\bi(?:'|\u2019)ll\b", re.I), "they will"), (re.compile(r"\bi(?:'|\u2019)d\b", re.I), "they would"),
    (re.compile(r"\bi was\b", re.I), "they were"), (re.compile(r"\bwe were\b", re.I), "they were"),
    (re.compile(r"\bwe\b", re.I), "they"), (re.compile(r"\bour\b", re.I), "their"), (re.compile(r"\bours\b", re.I), "theirs"),
    (re.compile(r"\bus\b", re.I), "them"), (re.compile(r"\bi\b"), "they"), (re.compile(r"\bmy\b", re.I), "their"),
    (re.compile(r"\bme\b", re.I), "them"), (re.compile(r"\bmyself\b", re.I), "themselves"),
]
_FILLER = re.compile(r"^\s*(?:so|well|yeah|yes|okay|ok|right|actually|honestly|basically|um|uh|and|but|also|just|"
                     r"thanks(?: for [^,.]*)?|got it|good to know|that's helpful|perfect)[,.\s]+", re.I)
_CHANNEL_TEXT = {Channel.teams: "Teams meeting", Channel.zoom: "Zoom meeting", Channel.genesys: "phone call",
                 Channel.mobile: "in-person meeting", Channel.upload: "recorded conversation"}


def third_person(sentence: str) -> str:
    out = sentence.strip()
    for _ in range(2):
        stripped = _FILLER.sub("", out)
        if stripped == out:
            break
        out = stripped
    for pattern, repl in _PRONOUNS:
        out = pattern.sub(repl, out)
    out = re.sub(r"\bthey (?:is|am)\b", "they are", out)
    out = re.sub(r"\bthey has\b", "they have", out)
    return out[:1].upper() + out[1:] if out else out


def _names(people: list[Participant]) -> str:
    names = [p.display_name for p in people]
    return names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1] if names else "the customer")


def _date_text(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


class RulesSummaryEngine:
    """Implements engines.contracts.SummaryEngine."""

    def summarize(self, ctx: ExtractionContext, *, products: list[ProductMention], topics: list[TopicTag],
                  tasks: list[TaskItem]) -> list[NoteSection]:
        meta = ctx.conversation.meta
        spoke = {s.speaker_id for s in ctx.policy.segments if s.text.strip()}
        customers = [p for p in meta.participants if p.id in spoke] or list(meta.participants)
        english = ctx.language == "en"
        usable = [s for s in ctx.policy.segments if s.label == "business" and s.text.strip()]
        used: set[str] = set()
        context_items = self._pick(usable, _CONTEXT_CUES, used) if english else []
        concern_items = self._pick(usable, _CONCERN_CUES, used, concerns=True) if english else []
        built: dict[str, NoteSection] = {}
        for sec in ctx.template.sections:
            kind = self._kind(sec)
            built[sec.key] = NoteSection(key=sec.key, title=sec.title)
            if kind == "overview":
                built[sec.key].text = self._overview(ctx, customers, products, topics, tasks)
            elif kind == "context":
                built[sec.key].items = context_items
            elif kind == "products":
                built[sec.key].items = [self._product_line(p) for p in products]
            elif kind == "concerns":
                built[sec.key].items = concern_items
            elif kind == "nextsteps":
                built[sec.key].items = [self._task_line(t) for t in tasks] or ["No follow-up actions were agreed on the call."]
            elif kind == "relationship":
                built[sec.key].items = [f.text for f in ctx.policy.relationship_facts] if ctx.template.include_relationship_context else []
            else:
                built[sec.key].text = None
        sections = [built[s.key] for s in ctx.template.sections]
        for sec, spec in zip(sections, ctx.template.sections, strict=True):
            if spec.kind == "list" and spec.max_items is not None:
                sec.items = sec.items[: spec.max_items]
        self._fit_budget(sections, ctx.template)
        return sections

    # ------------------------------------------------------------------ section routing
    @staticmethod
    def _kind(sec: TemplateSection) -> str:
        field = (sec.crm_field or "").lower()
        key = sec.key.lower()
        for token, kind in (("overview", "overview"), ("clientcontext", "context"), ("productsdiscussed", "products"),
                            ("concerns", "concerns"), ("nextsteps", "nextsteps"), ("relationshipnotes", "relationship")):
            if token in field:
                return kind
        if "overview" in key or "summary" in key:
            return "overview"
        if "context" in key or "situation" in key or "objective" in key:
            return "context"
        if "product" in key:
            return "products"
        if "concern" in key or "question" in key or "objection" in key:
            return "concerns"
        if "next" in key or "follow" in key or "action" in key:
            return "nextsteps"
        if "relationship" in key or "rapport" in key:
            return "relationship"
        return "other"

    # ------------------------------------------------------------------ text pieces
    def _overview(self, ctx: ExtractionContext, customers: list[Participant], products: list[ProductMention],
                  topics: list[TopicTag], tasks: list[TaskItem]) -> str:
        meta = ctx.conversation.meta
        org = meta.customer_org or next((p.org for p in customers if p.org), None)
        who = _names(customers)
        where = f" ({org})" if org else ""
        when = _date_text(meta.started_at.date())
        kind = _CHANNEL_TEXT.get(meta.source, "call")
        if meta.direction == Direction.inbound:
            first = f"{who}{where} called in and spoke with {meta.seller.display_name} ({when})."
        elif meta.source == Channel.mobile or meta.direction == Direction.in_person:
            first = f"{meta.seller.display_name} met {who}{where} in person on {when}."
        else:
            first = f"{meta.seller.display_name} spoke with {who}{where} on a {kind} on {when}."
        parts = [first]
        if ctx.language != "en":
            parts.append("The conversation was held in Spanish.")
        if topics:
            labels = [t.label.lower() for t in topics[:3]]
            parts.append("Discussion covered " + (", ".join(labels[:-1]) + " and " + labels[-1] if len(labels) > 1 else labels[0]) + ".")
        resolved = [m for m in products if m.chosen_product_id]
        if resolved:
            names = []
            by_id = {c.product_id: c.display_name for m in resolved for c in m.candidates}
            for m in resolved[:2]:
                names.append(by_id.get(m.chosen_product_id or "", m.surface))
            parts.append("Products in focus: " + " and ".join(names) + ".")
        elif products:
            parts.append(f"{len(products)} product mention(s) need a share class to be confirmed.")
        if tasks:
            parts.append(f"{len(tasks)} follow-up action{'s were' if len(tasks) > 1 else ' was'} agreed.")
        else:
            parts.append("No follow-up actions were agreed.")
        return " ".join(parts)

    @staticmethod
    def _product_line(m: ProductMention) -> str:
        stance = m.mention_type.value.replace("_", " ")
        if m.chosen_product_id and m.candidates:
            name = next((c.display_name for c in m.candidates if c.product_id == m.chosen_product_id), m.surface)
            return f"{name} - {stance}"
        return f"{m.surface} (share class to be confirmed) - {stance}"

    @staticmethod
    def _task_line(t: TaskItem) -> str:
        return t.title + (f" (due {t.due_date.isoformat()})" if t.due_date else "")

    # ------------------------------------------------------------------ extractive lists
    def _pick(self, segs: list[PolicySegment], cues: frozenset[str], used: set[str], *, concerns: bool = False) -> list[str]:
        scored: list[tuple[float, int, str]] = []
        order = 0
        for seg in segs:
            if seg.speaker_role != SpeakerRole.customer:
                continue
            for a, b in sentence_spans(seg.text):
                sentence = seg.text[a:b].strip()
                order += 1
                tokens = norm_words(sentence)
                key = " ".join(tokens)
                if len(tokens) < 7 or _SKIP_START.match(sentence) or "[" in sentence or key in used:
                    continue
                hits = float(len(set(tokens) & cues))
                if concerns:
                    score = hits + (1.0 if any(contains_phrase(key, p) for p in _CONCERN_PHRASES) else 0.0)
                    if score < 1.0:
                        continue
                    score += 0.5 if sentence.endswith("?") else 0.0
                else:
                    score = hits
                    if score < 1.0 or sentence.endswith("?"):
                        continue
                scored.append((score, order, sentence))
        scored.sort(key=lambda row: (-row[0], row[1]))
        chosen = sorted(scored[: 4 if concerns else 5], key=lambda row: row[1])
        items = []
        for _, _, sentence in chosen:
            used.add(" ".join(norm_words(sentence)))
            items.append(clip(third_person(sentence), 170))
        return items

    # ------------------------------------------------------------------ word budget
    @staticmethod
    def _fit_budget(sections: list[NoteSection], template: SummaryTemplate) -> None:
        def words() -> int:
            return sum(len((s.text or "").split()) + sum(len(i.split()) for i in s.items) for s in sections)

        droppable = [s for s in sections if s.key and any(t in s.key for t in ("concern", "question", "context", "situation"))]
        guard = 0
        while words() > template.max_words and guard < 30:
            guard += 1
            victim = max((s for s in droppable if len(s.items) > 1), key=lambda s: sum(len(i.split()) for i in s.items), default=None)
            if victim is None:
                break
            victim.items.pop()
        if words() > template.max_words:
            for s in sections:
                s.items = [clip(i, 110) for i in s.items]

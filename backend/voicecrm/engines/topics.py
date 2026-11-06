"""Rules-based topic classifier: maps the conversation onto the controlled taxonomy (the CRM dropdown values).

Scoring is lexical and transparent: every synonym hit (English + Spanish, accent-folded) adds weight - longer phrases
weigh more than single words, customer turns slightly more than seller turns - and a topic is tagged when its total
reaches the threshold. Each tag carries evidence (segment ids + a verbatim redacted quote).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import SpeakerRole
from ..domain.extraction import Evidence, TopicTag
from ..domain.taxonomy import Taxonomy, Topic
from .contracts import PolicySegment
from .text import find_sequence, norm_words, sentence_spans

_THRESHOLD = 1.0
_MAX_TOPICS = 5


@dataclass(slots=True)
class _Synonym:
    tokens: tuple[str, ...]
    weight: float


def _weight(tokens: tuple[str, ...]) -> float:
    return 0.5 if len(tokens) == 1 else (0.8 if len(tokens) == 2 else 1.0)


class RulesTopicEngine:
    def __init__(self, taxonomy: Taxonomy, *, threshold: float = _THRESHOLD, max_topics: int = _MAX_TOPICS) -> None:
        self.taxonomy = taxonomy
        self.threshold = threshold
        self.max_topics = max_topics
        self._synonyms: dict[str, list[_Synonym]] = {}
        for topic in taxonomy.topics:
            raw = [*topic.synonyms, *(s for lang in topic.synonyms_i18n.values() for s in lang)]
            seen: set[tuple[str, ...]] = set()
            syns: list[_Synonym] = []
            for phrase in raw:
                toks = tuple(norm_words(phrase))
                if toks and toks not in seen:
                    seen.add(toks)
                    syns.append(_Synonym(toks, _weight(toks)))
            self._synonyms[topic.topic_id] = syns

    def classify(self, segments: list[PolicySegment], *, language: str, business_line: str) -> list[TopicTag]:
        usable = [s for s in segments if s.text.strip() and s.label in ("business", "relationship")]
        scored: list[tuple[float, Topic, list[tuple[float, PolicySegment]]]] = []
        for topic in self.taxonomy.topics:
            if topic.business_lines and business_line not in topic.business_lines:
                continue
            total = 0.0
            hits: list[tuple[float, PolicySegment]] = []
            for seg in usable:
                words = norm_words(seg.text)
                seg_score = sum(syn.weight for syn in self._synonyms[topic.topic_id] if find_sequence(words, syn.tokens) >= 0)
                if seg_score <= 0:
                    continue
                seg_score = min(1.0, seg_score) * (1.15 if seg.speaker_role == SpeakerRole.customer else 1.0)
                total += seg_score
                hits.append((seg_score, seg))
            if total >= self.threshold:
                scored.append((total, topic, hits))
        scored.sort(key=lambda row: (-row[0], row[1].topic_id))
        tags: list[TopicTag] = []
        for total, topic, hits in scored[: self.max_topics]:
            best = sorted(hits, key=lambda h: (-h[0], h[1].id))[:2]
            tags.append(TopicTag(topic_id=topic.topic_id, label=topic.label, confidence=round(min(0.95, 0.5 + 0.12 * total), 3),
                                 evidence=[Evidence(segment_ids=[seg.id], quote=self._quote(seg.text, topic.topic_id))
                                           for _, seg in sorted(best, key=lambda h: h[1].id)]))
        return tags

    def _quote(self, text: str, topic_id: str) -> str:
        syns = self._synonyms[topic_id]
        for a, b in sentence_spans(text):
            words = norm_words(text[a:b])
            if any(find_sequence(words, s.tokens) >= 0 for s in syns):
                return text[a:b].strip()
        return text.strip()

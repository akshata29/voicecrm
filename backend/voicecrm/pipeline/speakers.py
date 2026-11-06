"""Speaker-to-participant mapping and dominant-language detection for normalising transcripts."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

from rapidfuzz import fuzz

from ..domain.conversation import ConversationMeta, Participant, Segment
from ..domain.enums import Direction, SpeakerRole

_SELLER_LABELS = {"agent", "internal", "seller", "rep", "representative", "advisor", "sales"}
_CUSTOMER_LABELS = {"customer", "external", "client", "caller", "prospect"}
_ES_STOPWORDS = {
    "el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "por", "con", "para", "es", "te", "mi", "si",
    "gracias", "hola", "buenos", "días", "muy", "como", "pero", "nos", "le", "se", "lo", "al", "del", "esta", "tengo",
}
_EN_STOPWORDS = {
    "the", "and", "of", "to", "in", "is", "that", "it", "for", "we", "you", "with", "on", "are", "this", "have",
    "thanks", "hello", "yes", "so", "but", "our", "be", "can", "would", "just",
}


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", value.lower()).strip()


def _match_participant(label: str, people: list[Participant]) -> Participant | None:
    n = _norm(label)
    if not n:
        return None
    for p in people:
        if p.id == label or _norm(p.display_name) == n:
            return p
    best: tuple[float, Participant | None] = (0.0, None)
    for p in people:
        pn = _norm(p.display_name)
        if not pn:
            continue
        score = max(fuzz.token_set_ratio(n, pn), 100 if n.split()[0] == pn.split()[0] and len(n.split()[0]) > 2 else 0)
        if score > best[0]:
            best = (score, p)
    return best[1] if best[0] >= 85 else None


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


_INTRO = r"(?:this is|it'?s|it is|i'?m|i am|my name is|you'?re speaking (?:with|to)|soy|habla)"
_GREET = (r"(?:hi|hello|hey|hola|thanks|thank you|gracias|good morning|good afternoon|good evening|morning|afternoon|"
          r"buenos dias|buenas tardes|great|sure|okay|ok|right|well|yes)")


def _evidence(text: str, person: Participant) -> int:
    """How strongly `text` (everything one diarized speaker said) points at that person being the speaker.

    +3 for introducing themselves by first name ("it's Sam", "Casey here", "this is Jordan from ..."), -2 for greeting or
    thanking them by name (you do not greet yourself): "Hi Dana" says the speaker is NOT Dana.
    """
    first = next((tok for tok in _norm(person.display_name).split() if len(tok) > 2), None)
    if first is None:
        return 0
    low = _fold(text)
    score = 0
    if re.search(rf"\b{_INTRO}\s+(?:\w+\s+)?{first}\b", low) or re.search(rf"\b{first}\s+(?:here|speaking)\b", low):
        score += 3
    if re.search(rf"\b{_GREET}[,!.]?\s+(?:(?:and|y)\s+)?{first}\b", low) or re.search(rf"(?:^|[.?!]\s+){first},", low):
        score -= 2
    return score


def _seller_label(unresolved: list[str], text_by_label: dict[str, str], seller: Participant, customers: list[Participant]) -> str | None:
    """The unresolved label that most looks like the seller, or None when the transcript gives no name evidence."""
    best: tuple[int, str | None] = (0, None)
    for label in unresolved:
        text = text_by_label.get(label, "")
        lean = _evidence(text, seller) - max((_evidence(text, c) for c in customers), default=0)
        if lean > best[0]:
            best = (lean, label)
    return best[1]


def map_speakers(segments: list[Segment], meta: ConversationMeta) -> tuple[list[Segment], list[Participant]]:
    """Rewrites Segment.speaker_id to participant ids. Returns (segments, participants actually referenced).

    Order of resolution: exact id / name match, role words (agent / customer), then - for diarized labels such as
    "S1", "S2" - who introduces or addresses whom by name, and finally position: the first unresolved speaker is the
    seller (the rep opens a meeting, and a contact-centre agent greets first on an inbound call). A transcript with a
    single speaker is the customer on an inbound call and the seller otherwise.
    """
    seller = meta.seller
    customers = list(meta.participants) or [Participant(id="cust1", display_name="Customer", role=SpeakerRole.customer)]
    everyone = [seller, *customers]
    labels: list[str] = []
    text_by_label: dict[str, str] = {}
    for s in segments:
        if s.speaker_id not in labels:
            labels.append(s.speaker_id)
        text_by_label[s.speaker_id] = f"{text_by_label.get(s.speaker_id, '')} {s.text}"

    mapping: dict[str, str] = {}
    for label in labels:
        person = _match_participant(label, everyone)
        if person:
            mapping[label] = person.id
            continue
        low = _norm(label)
        if low in _SELLER_LABELS:
            mapping[label] = seller.id
        elif low in _CUSTOMER_LABELS:
            mapping[label] = customers[0].id

    unresolved = [lab for lab in labels if lab not in mapping]
    inbound = meta.direction == Direction.inbound
    if seller.id not in mapping.values() and unresolved:
        if len(unresolved) == 1:
            customers_all_placed = all(c.id in mapping.values() for c in customers)
            seller_label = unresolved[0] if (not inbound or customers_all_placed) else None
        else:
            seller_label = _seller_label(unresolved, text_by_label, seller, customers) or unresolved[0]
        if seller_label is not None:
            mapping[seller_label] = seller.id
    for label in [lab for lab in unresolved if lab not in mapping]:
        taken = set(mapping.values())
        pick = next((p for p in customers if p.id not in taken), customers[-1])
        mapping[label] = pick.id

    mapped = [s.model_copy(update={"speaker_id": mapping.get(s.speaker_id, s.speaker_id)}) for s in segments]
    used = {s.speaker_id for s in mapped}
    return mapped, [p for p in everyone if p.id in used]


def detect_language(segments: list[Segment], hint: str | None) -> str:
    """Dominant BCP-47 language: segment locales first, then a stopword heuristic, then the hint, then en-US."""
    locales = Counter((s.locale or "").strip() for s in segments if s.locale)
    if locales:
        top = locales.most_common(1)[0][0]
        if top:
            return top
    words = [w for s in segments for w in re.findall(r"[a-záéíóúñü']+", s.text.lower())]
    if words:
        es = sum(w in _ES_STOPWORDS for w in words)
        en = sum(w in _EN_STOPWORDS for w in words)
        if es > en * 1.2 and es / max(len(words), 1) > 0.08:
            return "es-US"
        if en >= es:
            return hint or "en-US"
    return hint or "en-US"


def short_language(bcp47: str) -> str:
    return (bcp47 or "en").split("-")[0].lower()

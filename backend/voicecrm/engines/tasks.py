"""Rules-based commitment extractor: "I'll send you the fee comparison by Friday" -> a dated seller task.

Only commitments made by the seller or their colleagues become tasks (customer promises are context, not CRM tasks).
English and Spanish. Relative dates are resolved against the call date with one documented convention:

    "tomorrow" +1 | "today"/"EOD" +0 | "<weekday>" / "by <weekday>" = the next such weekday strictly after the call date
    "next <weekday>" = that weekday of the following Mon-Sun week | "end of the week" = Friday of this week
    "end of next week" = Friday of next week | "in N days/weeks" = +N | "next week" alone = no date (too vague)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from ..domain.conversation import ConversationMeta
from ..domain.enums import OwnerRole, SpeakerRole
from ..domain.extraction import Evidence, TaskItem
from .contracts import PolicySegment
from .text import clip, fold, norm_text, norm_words, sentence_spans

_EN_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ES_DAYS = ["lunes", "martes", "mi[eé]rcoles", "jueves", "viernes", "s[aá]bado", "domingo"]
_EN_DAY_RE = "|".join([*_EN_DAYS, "tues", "thurs"])
_ES_DAY_RE = "|".join(_ES_DAYS)
_NUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "un": 1, "una": 1,
        "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "diez": 10}


@dataclass(slots=True)
class Due:
    day: date | None
    text: str | None
    basis: str                     # stated | inferred | none
    span: tuple[int, int] | None = None


def _weekday_index(name: str) -> int:
    n = fold(name)
    for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]):
        if n.startswith(d[:3]) or n.startswith({0: "lun", 1: "mar", 2: "mie", 3: "jue", 4: "vie", 5: "sab", 6: "dom"}[i]):
            if n.startswith("tue") or n.startswith("thu") or n.startswith("mon") or n.startswith("wed") or n.startswith("fri") \
                    or n.startswith("sat") or n.startswith("sun") or len(n) >= 5:
                return i
    return 0


def _next_weekday(anchor: date, wd: int) -> date:
    delta = (wd - anchor.weekday()) % 7
    return anchor + timedelta(days=delta or 7)


def _following_week(anchor: date, wd: int) -> date:
    monday_next = anchor + timedelta(days=7 - anchor.weekday())
    return monday_next + timedelta(days=wd)


def _friday_this_week(anchor: date) -> date:
    delta = 4 - anchor.weekday()
    return anchor + timedelta(days=delta if delta >= 0 else delta + 7)


_EOD = r"(?:by\s+)?(?:the\s+)?(?:eod|cob|end of (?:the )?day|close of business)"
_DUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "by EOD Friday" / "by COB tomorrow": the named day wins over the end-of-day marker
    (re.compile(rf"\b{_EOD}\s+(?:on\s+)?next\s+(?P<d>{_EN_DAY_RE})\b", re.I), "next_wd"),
    (re.compile(rf"\b{_EOD}\s+(?:on\s+)?(?P<d>{_EN_DAY_RE})\b", re.I), "wd"),
    (re.compile(rf"\b{_EOD}\s+tomorrow\b", re.I), "plus1"),
    (re.compile(r"\bday after tomorrow\b|\bpasado ma[nñ]ana\b", re.I), "plus2"),
    (re.compile(r"\btomorrow\b|(?<!\bla )(?<!\besta )(?<!\bpor la )(?<!\bde la )(?<!\ben la )\bma[nñ]ana\b", re.I), "plus1"),
    (re.compile(r"\blater today\b|\btoday\b|\bthis (?:afternoon|evening)\b|\btonight\b|\bby (?:the )?(?:end of (?:the )?day|eod|"
                r"close of business|cob)\b|\beod\b|\bhoy(?: mismo)?\b|\besta (?:tarde|noche)\b", re.I), "today"),
    (re.compile(r"\b(?:by )?(?:the )?end of next week\b|\ba (?:finales|fines) de la pr[oó]xima semana\b|"
                r"\bfinales de la pr[oó]xima semana\b", re.I), "eonw"),
    (re.compile(r"\b(?:by )?(?:the )?end of (?:the |this )?week\b|\beow\b|\ba (?:finales|fines) de (?:la |esta )?semana\b|"
                r"\bantes del fin de semana\b|\bpara el fin de semana\b", re.I), "eow"),
    (re.compile(r"\bin (?P<n>a couple of|a few|two|three|four|five|six|seven|ten|\d+|one|a) (?P<u>day|days|week|weeks)\b|"
                r"\ben (?P<n2>dos|tres|cuatro|cinco|seis|siete|diez|un par de|unos|\d+|una|un) (?P<u2>d[ií]as?|semanas?)\b", re.I),
     "in_n"),
    (re.compile(rf"\bnext (?P<d>{_EN_DAY_RE})\b|\bel pr[oó]ximo (?P<d2>{_ES_DAY_RE})\b|\bel (?P<d3>{_ES_DAY_RE}) "
                rf"(?:pr[oó]ximo|que viene)\b", re.I), "next_wd"),
    (re.compile(rf"\b(?:(?:by|on|before|until|this|for)\s+)?(?:this\s+)?(?P<d>{_EN_DAY_RE})\b|\b(?:el|este|para el|antes del|"
                rf"hasta el)\s+(?P<d2>{_ES_DAY_RE})\b", re.I), "wd"),
]
_VAGUE = re.compile(r"\bnext week\b|\bla pr[oó]xima semana\b|\bsoon\b|\bpronto\b|\bin a bit\b", re.I)


def resolve_due(text: str, anchor: date) -> Due:
    """First date expression in `text`, resolved against `anchor` (see module docstring for the convention)."""
    best: tuple[int, Due] | None = None
    for pattern, kind in _DUE_PATTERNS:
        m = pattern.search(text)
        if not m or (best is not None and m.start() >= best[0]):
            continue
        phrase, span = m.group(0), (m.start(), m.end())
        if kind == "plus1":
            due = Due(anchor + timedelta(days=1), phrase, "stated", span)
        elif kind == "plus2":
            due = Due(anchor + timedelta(days=2), phrase, "stated", span)
        elif kind == "today":
            due = Due(anchor, phrase, "stated", span)
        elif kind == "eonw":
            due = Due(_friday_this_week(anchor + timedelta(days=7 - anchor.weekday())), phrase, "stated", span)
        elif kind == "eow":
            due = Due(_friday_this_week(anchor), phrase, "stated", span)
        elif kind == "in_n":
            raw = (m.group("n") or m.group("n2") or "").lower()
            unit = (m.group("u") or m.group("u2") or "").lower()
            vague = raw in {"a couple of", "a few", "un par de", "unos"}
            count = {"a couple of": 2, "a few": 3, "un par de": 2, "unos": 3}.get(raw) or (int(raw) if raw.isdigit() else _NUM.get(raw, 1))
            days = count * (7 if unit.startswith(("week", "semana")) else 1)
            due = Due(anchor + timedelta(days=days), phrase, "inferred" if vague else "stated", span)
        elif kind == "next_wd":
            gd = m.groupdict()
            name = gd.get("d") or gd.get("d2") or gd.get("d3")
            due = Due(_following_week(anchor, _weekday_index(name)), phrase, "stated", span)
        else:
            gd = m.groupdict()
            name = gd.get("d") or gd.get("d2")
            due = Due(_next_weekday(anchor, _weekday_index(name)), phrase, "stated", span)
        if best is None or m.start() < best[0]:
            best = (m.start(), due)
    if best is not None:
        return best[1]
    vague = _VAGUE.search(text)
    return Due(None, vague.group(0) if vague else None, "none", vague.span() if vague else None)


# ---- commitment detection -------------------------------------------------------------------------------------
_VERBS = (r"send|share|forward|email|e-mail|circulate|follow\s?up|schedule|set\s?up|arrange|book|put\s+together|prepare|draft|"
          r"check|confirm|get\s+back|reach\s+out|connect|introduce|loop\s+in|open|escalate|raise|file|provide|pull|run|update|"
          r"resend|look\s+into|investigate|call|ring|phone|bring|walk|review|find\s+out|come\s+back|revert|coordinate|"
          r"organi[sz]e|get(?=\s+(?:the|a|an|that|this|it|you|them|those|these|back|your|our|some|my)\b)|make\s+sure|line\s+up|"
          r"compile|assemble|deliver|submit|reschedule|add|invite")
_WILL = r"(?:'ll|\u2019ll|\s+will|\s+shall|\s+(?:am|are)\s+going\s+to|'m\s+going\s+to|\u2019m\s+going\s+to)"
_COMMIT_EN = re.compile(rf"\b(?:i|we){_WILL}\s+(?:(?:also|then|definitely|certainly|just|absolutely|promptly|quickly|personally|"
                        rf"try\s+to|make\s+sure\s+to|be\s+sure\s+to)\s+)*(?P<verb>{_VERBS})\b", re.I)
_LETME_EN = re.compile(rf"\blet\s+me\s+(?:(?:also|just|quickly)\s+)?(?P<verb>{_VERBS}|circle\s+back)\b", re.I)
_TEAM_EN = re.compile(rf"\b(?:(?i:our\s+[\w\s]{{2,30}}?team|the\s+[\w\s]{{2,30}}?team)|[A-Z][a-z]+)\s+(?i:will|is\s+going\s+to)\s+"
                      rf"(?i:(?:(?:also|then)\s+)?)(?P<verb>(?i:{_VERBS}))\b")
_HAVE_EN = re.compile(rf"\b(?:i|we){_WILL}\s+have\s+(?P<who>[\w' ]{{2,30}}?)\s+(?P<verb>send|prepare|follow\s?up|reach\s+out|call|"
                      rf"set\s?up|schedule|get|put\s+together|email)\b", re.I)
_OFFER_EN = re.compile(rf"\b(?:i|we)\s+(?:can|could|would\s+be\s+happy\s+to|d\s+be\s+happy\s+to|'d\s+be\s+happy\s+to)\s+"
                       rf"(?P<verb>{_VERBS})\b|\bi'd\s+be\s+happy\s+to\s+(?P<verb2>{_VERBS})\b", re.I)
_VERB_AFTER_AND = re.compile(rf"\b(?:,\s*)?(?:and|then|and\s+then)\s+(?:i(?:'ll|\s+will)\s+|we(?:'ll|\s+will)\s+)?(?:also\s+)?"
                             rf"(?P<verb>{_VERBS})\b", re.I)

_ES_ACTION = {
    "envio": "Send", "enviare": "Send", "enviamos": "Send", "mando": "Send", "mandare": "Send", "comparto": "Share",
    "compartire": "Share", "preparo": "Prepare", "preparare": "Prepare", "programo": "Schedule", "programare": "Schedule",
    "agendo": "Schedule", "agendare": "Schedule", "coordino": "Coordinate", "coordinare": "Coordinate", "organizo": "Organize",
    "organizare": "Organize", "confirmo": "Confirm", "confirmare": "Confirm", "reviso": "Review", "revisare": "Review",
    "llamo": "Call", "llamare": "Call", "escribo": "Write to", "escribire": "Write to", "enviar": "Send", "mandar": "Send",
    "compartir": "Share", "preparar": "Prepare", "programar": "Schedule", "agendar": "Schedule", "coordinar": "Coordinate",
    "organizar": "Organize", "confirmar": "Confirm", "revisar": "Review", "llamar": "Call", "escribir": "Write to",
    "hago llegar": "Send", "hare llegar": "Send", "doy seguimiento": "Follow up on", "dare seguimiento": "Follow up on",
    "agrego": "Add", "agregare": "Add", "incluyo": "Include", "incluire": "Include", "preparaemos": "Prepare",
}
_ES_COMMIT = re.compile(
    r"\b(?:(?:le|te|les|se|me)\s+)?(?:lo\s+|la\s+|los\s+|las\s+)?(?P<verb>env[ií]o|enviar[eé]|enviamos|mando|mandar[eé]|comparto|"
    r"compartir[eé]|preparo|preparar[eé]|programo|programar[eé]|agendo|agendar[eé]|coordino|coordinar[eé]|organizo|organizar[eé]|"
    r"confirmo|confirmar[eé]|reviso|revisar[eé]|llamo|llamar[eé]|escribo|escribir[eé]|hago\s+llegar|har[eé]\s+llegar|"
    r"doy\s+seguimiento|dar[eé]\s+seguimiento|agrego|agregar[eé]|incluyo|incluir[eé])\b"
    r"|\b(?:voy\s+a|vamos\s+a|me\s+comprometo\s+a|quedo\s+de|nos\s+comprometemos\s+a)\s+(?:(?:le|te|les)\s+)?"
    r"(?P<verb2>enviar|mandar|compartir|preparar|programar|agendar|coordinar|organizar|confirmar|revisar|llamar|escribir)"
    r"(?:le|te|les|lo|la)?\b", re.I)
_ES_GLOSSARY = {
    "comparacion de comisiones": "fee comparison", "comparacion de costos": "cost comparison", "comparativo de comisiones": "fee comparison",
    "ficha tecnica": "fact sheet", "fichas tecnicas": "fact sheets", "presentacion": "presentation", "propuesta": "proposal",
    "reunion de seguimiento": "follow-up meeting", "reunion": "meeting", "llamada": "call", "informe": "report",
    "documento": "document", "documentacion": "documentation", "folleto": "brochure", "prospecto": "prospectus",
    "cuestionario": "questionnaire", "datos de rendimiento": "performance data", "rendimiento": "performance",
    "gestor de cartera": "portfolio manager", "analisis": "analysis", "comparativo": "comparison", "comparacion": "comparison",
    "cotizacion": "quote", "invitacion": "invitation", "resumen": "summary", "materiales": "materials", "informacion": "information",
    "detalles": "details", "comisiones": "fees", "comision": "fee", "estructura de comisiones": "fee structure",
    "con el": "with the", "con la": "with the", "con": "with", "al": "to the", "de": "", "el": "the", "la": "the", "los": "the",
    "las": "the", "sobre": "on", "para": "for", "y": "and", "un": "a", "una": "a", "unos": "some", "unas": "some",
    "tecnica": "", "nuestro": "our", "nuestra": "our", "su": "your", "sus": "your",
}
_ACCEPT = frozenset({"yes", "yeah", "please", "sure", "great", "perfect", "sounds", "good", "helpful", "appreciate", "appreciated",
                     "gracias", "si", "claro", "favor", "excelente", "genial", "that", "would", "be"})
_HEDGE = re.compile(r"\b(?:maybe|might|probably|perhaps|if i can|see if i can|quiz[aá]s|tal vez|a lo mejor)\b", re.I)
_NEGATED = re.compile(r"\b(?:won't|will not|can't|cannot|not going to|don't|no voy a|no puedo)\b", re.I)


_PRONOUN_TAILS = frozenset({"that", "it", "this", "them", "those", "these", "one", "lo", "la", "los", "las", "eso", "esto", "ello",
                            "all of it", "everything"})
_OBJECTS_EN = ("fee comparison", "fee schedule", "fee waiver details", "fact sheet", "factsheet", "fact sheets", "one-pager",
               "one pager", "presentation", "deck", "pitch book", "proposal", "white paper", "whitepaper", "report", "document",
               "questionnaire", "ddq", "checklist", "conversion form", "commentary", "performance data", "holdings file",
               "prospectus", "brochure", "materials", "summary", "analysis", "comparison", "invitation", "agenda")
_OBJECTS_ES = ("ficha tecnica", "fichas tecnicas", "comparacion de comisiones", "presentacion", "propuesta", "informe", "documento",
               "folleto", "prospecto", "cuestionario", "datos de rendimiento", "analisis", "resumen", "materiales", "informacion",
               "detalles", "invitacion", "cotizacion", "comparativo")


_ES_OBJECT_KEYS = ("comparacion de comisiones", "comparacion de costos", "comparativo de comisiones", "ficha tecnica", "fichas tecnicas",
                   "presentacion", "propuesta", "reunion de seguimiento", "reunion", "llamada", "informe", "documento",
                   "documentacion", "folleto", "prospecto", "cuestionario", "datos de rendimiento", "analisis", "comparativo",
                   "cotizacion", "invitacion", "resumen", "materiales", "estructura de comisiones", "gestor de cartera")


def _titlecase_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _translate(tail: str) -> str:
    """Spanish object phrase -> English words via the glossary (longest phrase first); unknown words pass through."""
    words = tail.split()
    folded = [fold(w) for w in words]
    out: list[str] = []
    i = 0
    keys = sorted(_ES_GLOSSARY, key=lambda k: -len(k.split()))
    while i < len(words):
        for key in keys:
            parts = key.split()
            if folded[i:i + len(parts)] == parts:
                if _ES_GLOSSARY[key]:
                    out.append(_ES_GLOSSARY[key])
                i += len(parts)
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


class RulesTaskEngine:
    """Implements engines.contracts.TaskEngine."""

    def extract(self, segments: list[PolicySegment], meta: ConversationMeta, *, language: str) -> list[TaskItem]:
        anchor = meta.started_at.date()
        customer = meta.participants[0].display_name if meta.participants else None
        blocked = frozenset(w for p in meta.participants for w in norm_words(p.display_name)) | {"you", "your", "customer", "client"}
        tasks: list[TaskItem] = []
        seen: set[str] = set()
        usable = [s for s in segments if s.text.strip() and s.label in ("business", "relationship")]
        for pos, seg in enumerate(usable):
            if seg.speaker_role not in (SpeakerRole.seller, SpeakerRole.internal):
                continue
            for a, b in sentence_spans(seg.text):
                sentence = seg.text[a:b].strip()
                if not sentence or sentence.endswith("?") or _NEGATED.search(sentence):
                    continue
                for title, description, due, conf in self._commitments(sentence, anchor, customer, usable, pos, blocked):
                    key = " ".join(norm_words(title))
                    if key in seen:
                        continue
                    seen.add(key)
                    owner = OwnerRole.internal if seg.speaker_role == SpeakerRole.internal else OwnerRole.seller
                    tasks.append(TaskItem(
                        task_id=f"t{len(tasks) + 1}", title=title, description=description, owner_role=owner,
                        owner_ref=seg.speaker_id, due_date=due.day, due_text=due.text if due.day else None, due_basis=due.basis,
                        confidence=conf, evidence=[Evidence(segment_ids=[seg.id], quote=sentence)]))
        return tasks

    # ------------------------------------------------------------------ one sentence -> 0..n commitments
    def _commitments(self, sentence: str, anchor: date, customer: str | None, usable: list[PolicySegment],
                     pos: int, blocked: frozenset[str] = frozenset()) -> list[tuple[str, str, Due, float]]:
        out: list[tuple[str, str, Due, float]] = []
        hedged = bool(_HEDGE.search(sentence)) and not re.search(r"\bi'?ll try\b", sentence, re.I)
        if hedged:
            return out
        spanish = bool(_ES_COMMIT.search(sentence)) and not _COMMIT_EN.search(sentence)
        sentence_due = resolve_due(sentence, anchor)
        starts_conditional = bool(re.match(r"\s*(?:if|once|when|si|cuando)\b", sentence, re.I))
        matches: list[tuple[int, int, str, bool]] = []                 # start, end-of-verb, verb, is_offer
        if spanish:
            for m in _ES_COMMIT.finditer(sentence):
                verb = fold(m.group("verb") or m.group("verb2") or "")
                matches.append((m.start(), m.end(), verb, bool(re.search(r"\bpuedo\b", sentence[:m.start()], re.I))))
        else:
            for pattern in (_HAVE_EN, _COMMIT_EN, _LETME_EN, _TEAM_EN):
                for m in pattern.finditer(sentence):
                    if pattern is _TEAM_EN and set(norm_words(sentence[m.start():m.start("verb")])) & blocked:
                        continue                                       # "Dana will send ..." is the customer's promise
                    if not any(s <= m.start() < e for s, e, _, _ in matches):
                        matches.append((m.start(), m.end(), m.group("verb").lower(), starts_conditional))
            for m in _OFFER_EN.finditer(sentence):
                if not any(s <= m.start() < e for s, e, _, _ in matches):
                    matches.append((m.start(), m.end(), (m.group("verb") or m.group("verb2")).lower(), True))
        matches.sort()
        context = [sentence] + [s.text for s in (usable[pos], *reversed(usable[max(0, pos - 2):pos])) if s.text]
        for idx, (start, vend, verb, offer) in enumerate(matches):
            end = matches[idx + 1][0] if idx + 1 < len(matches) else len(sentence)
            tail = sentence[vend:end]
            if offer and not self._accepted(usable, pos):
                continue
            for part_idx, (part_verb, part_tail) in enumerate(self._split_compound(verb, tail)):
                title, due = self._title(part_verb, part_tail, anchor, customer, spanish, context, later_clause=part_idx > 0)
                if not title:
                    continue
                if part_idx > 0 and title.startswith("\x00"):             # "... and escalate it to operations": same deliverable
                    if out:
                        merged = clip(f"{out[-1][0]} and {title[1:]}", 110)
                        out[-1] = (merged, out[-1][1], out[-1][2], out[-1][3])
                    continue
                if due.day is None and sentence_due.day is not None and len(matches) == 1:
                    due = sentence_due
                conf = 0.7 if offer else 0.82
                if due.day is not None:
                    conf += 0.1
                if re.search(r"\btry\b", sentence[max(0, start - 12):vend + 8], re.I):
                    conf -= 0.1
                out.append((title, clip(sentence, 240), due, round(min(conf, 0.97), 3)))
        return out

    @staticmethod
    def _accepted(usable: list[PolicySegment], pos: int) -> bool:
        for nxt in usable[pos + 1: pos + 3]:
            if nxt.speaker_role == SpeakerRole.customer and set(norm_words(nxt.text)) & _ACCEPT:
                return True
        return False

    @staticmethod
    def _split_compound(verb: str, tail: str) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = []
        cut = 0
        current = verb
        for m in _VERB_AFTER_AND.finditer(tail):
            parts.append((current, tail[cut:m.start()]))
            cut, current = m.end(), m.group("verb").lower()
        parts.append((current, tail[cut:]))
        return parts

    @staticmethod
    def _spanish_objects(tail: str) -> str:
        """The promised materials named in a Spanish clause, in English ('la ficha técnica y la presentación' -> 'fact sheet and presentation').

        Unknown nouns are left out on purpose: a short, clean English title beats a half-translated one (the full Spanish
        sentence stays in the task description)."""
        norm = " " + norm_text(tail) + " "
        found: list[tuple[int, str]] = []
        for key in sorted(_ES_OBJECT_KEYS, key=lambda k: -len(k.split())):
            idx = norm.find(" " + key + " ")
            if idx >= 0 and not any(abs(idx - other) < len(key) for other, _ in found):
                found.append((idx, _ES_GLOSSARY[key]))
        found.sort()
        phrases = list(dict.fromkeys(p for _, p in found))
        if phrases:
            return " and ".join(phrases)
        return _translate(" ".join(tail.split()[:5])).strip(" ,.")

    @staticmethod
    def _find_object(context: list[str], spanish: bool) -> str | None:
        """'Let me get that over to you' / 'se la envío': the material being promised, from the sentence or recent turns."""
        nouns = _OBJECTS_ES if spanish else _OBJECTS_EN
        for text in context:
            norm = " " + norm_text(text) + " "
            best: tuple[int, str] | None = None
            for noun in nouns:
                idx = norm.rfind(" " + norm_text(noun) + " ")
                if idx >= 0 and (best is None or idx > best[0]):
                    best = (idx, noun)
            if best is not None:
                return _ES_GLOSSARY.get(norm_text(best[1]), best[1]) if spanish else best[1]
        return None

    def _title(self, verb: str, tail: str, anchor: date, customer: str | None, spanish: bool,
               context: list[str] | None = None, *, later_clause: bool = False) -> tuple[str, Due]:
        due = resolve_due(tail, anchor)
        if due.span:
            tail = tail[:due.span[0]] + " " + tail[due.span[1]:]
        tail = re.split(r"[.;!?]| so that | because | which | since |, and (?=i|we)|, but |, with |, including |, along with |"
                        r", covering |, showing |, so ", tail, maxsplit=1)[0]
        tail = re.sub(r"\b(?:over\s+|across\s+)?to\s+you\b|\byou\s+(?:with|on)\b|^\s*(?:you|te|le|les)\b", " ", tail, flags=re.I)
        tail = re.sub(r"\s+(?:by|on|before|until|for|el|para|antes del)\s*$", "", tail.strip(" ,"), flags=re.I)
        tail = re.sub(r"\s+(?:by\s+)?(?:eod|cob|end of (?:the )?day|close of business)\s*$", "", tail.strip(" ,"), flags=re.I)
        tail = re.sub(r"^\s*(?:you|up|back|me)\b\s*", "", tail, flags=re.I).strip(" ,")
        tail = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s+", " ", tail))
        if spanish:
            action = _ES_ACTION.get(fold(verb)) or _ES_ACTION.get(" ".join(fold(w) for w in verb.split()), "Follow up on")
            tail = self._spanish_objects(tail)
        else:
            action = _titlecase_first(re.sub(r"\s+", " ", verb.replace("e-mail", "email")))
            action = {"Get back": "Get back to the customer", "Reach out": "Reach out to the customer",
                      "Circle back": "Circle back to the customer", "Come back": "Get back to the customer",
                      "Loop in": "Loop in", "Followup": "Follow up", "Setup": "Set up", "Walk": "Walk through"}.get(action, action)
        tail = re.sub(r"\s+and$", "", tail).strip(" ,-")
        if later_clause:
            first, _, rest = tail.partition(" ")
            if first.lower() in _PRONOUN_TAILS or not tail:
                # "... and send it to you": the same deliverable, not a new task; "and escalate it to operations" extends it
                return ("" if not rest.strip() or re.fullmatch(r"(?:over|across|along|your way|there)", rest.strip(), re.I)
                        else f"\x00{action.lower()} {tail}"), due
        if action in {"Get back to the customer", "Reach out to the customer", "Circle back to the customer"}:
            tail = re.sub(r"^(?:to|with)\s+", "", tail)
            tail = f"with {tail}" if tail and not tail.lower().startswith(("on ", "about ", "regarding ", "with ")) else tail
            action = action.replace(" the customer", "")
        if tail.lower() in _PRONOUN_TAILS or not tail:
            found = self._find_object(context or [], spanish)
            if found:
                tail = found
        if not tail:
            if action.lower().startswith(("follow up", "get back", "reach out", "circle back")) and customer:
                return f"{action} with {customer}", due
            return (f"{action}" if len(action) > 4 else ""), due
        return clip(f"{action} {tail}", 110), due

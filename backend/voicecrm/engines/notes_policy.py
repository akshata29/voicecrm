"""Rules-based notes policy: what may enter a CRM note.

Applies, in order, to every transcript segment: (1) PII spans from the PII port are replaced by category placeholders;
(2) segments disclosing medical / family-health information are omitted entirely (the placeholder keeps only the
category); (3) rapport facts that are safe to keep (sports teams, hobbies, hometown, contact preferences) are captured
as `RelationshipFact`s; (4) greetings, weather and other small talk are labelled so extraction ignores them.

Nothing in `PolicyAction.detail` ever contains transcript text - the audit trail names categories and counts only.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..domain.conversation import CanonicalConversation, Segment
from ..domain.enums import SpeakerRole
from ..domain.extraction import Evidence, PolicyAction, RelationshipFact
from ..domain.policy import NotesPolicy
from ..ports.pii import PiiSpan
from .contracts import NotesPolicyResult, PolicySegment, SegmentLabel
from .text import contains_phrase, norm_text, norm_words, sentences

# ---- sensitive disclosures ---------------------------------------------------------------------------------------
_MEDICAL_STRONG = frozenset({
    "cancer", "chemotherapy", "chemo", "oncologist", "oncology", "tumor", "tumour", "leukemia", "lymphoma", "dementia",
    "alzheimers", "alzheimer", "parkinsons", "dialysis", "biopsy", "mastectomy", "hospice", "quimioterapia", "quimio",
    "demencia", "dialisis", "biopsia", "leucemia",
})
_MEDICAL_WEAK = frozenset({
    "surgery", "surgeon", "diagnosed", "diagnosis", "stroke", "hospitalized", "hospitalised", "diabetes", "diabetic",
    "depression", "anxiety", "pregnant", "pregnancy", "miscarriage", "rehab", "rehabilitation", "transplant", "cirugia",
    "diagnosticado", "diagnosticada", "embarazada", "depresion", "ansiedad", "enfermedad", "enferma", "enfermo",
})
_MEDICAL_PHRASES = (
    "heart attack", "heart surgery", "mental health", "passed away", "in the hospital", "hospital stay", "health scare",
    "health issue", "health issues", "health problems", "medical condition", "medical leave", "terminally ill",
    "problemas de salud", "ataque al corazon", "ha fallecido", "falleció", "fallecio",
)
_FAMILY = frozenset({
    "wife", "husband", "son", "daughter", "mother", "father", "mom", "dad", "brother", "sister", "partner", "spouse",
    "parents", "kids", "child", "children", "grandmother", "grandfather", "esposa", "esposo", "hijo", "hija", "madre",
    "padre", "mama", "papa", "hermano", "hermana", "abuela", "abuelo",
})
_PERSONAL_CONTEXT = _FAMILY | frozenset({"my", "his", "her", "their", "i", "she", "he", "ive", "im", "myself", "mi", "su",
                                         "me", "mis"})

# ---- small talk -----------------------------------------------------------------------------------------------------
_SMALL_TALK_PHRASES = (
    "good morning", "good afternoon", "good evening", "how are you", "hows it going", "how is it going", "nice to meet you",
    "thanks for joining", "thank you for joining", "thanks for making time", "thanks for taking the time", "can you hear me",
    "you are on mute", "youre on mute", "let me share my screen", "have a great", "have a good", "take care", "talk soon",
    "see you", "enjoy the", "how was your", "hows the weather", "weather", "weekend", "vacation", "holiday", "traffic",
    "coffee", "lunch", "dinner", "menu", "waiter", "waitress", "order the", "buenos dias", "buenas tardes", "buenas noches",
    "como esta", "como estas", "que tal", "fin de semana", "clima", "que tengas", "nos vemos", "hasta luego", "gracias por",
    "game last night", "did you watch", "did you catch", "did you see the game", "long time no see", "good to see you",
    "good to hear from you", "hope you are well", "hope youre well", "recording", "grabando",
)
_SMALL_TALK_TOKENS = frozenset({"hello", "hi", "hey", "bye", "goodbye", "hola", "adios", "rain", "raining", "sunny", "snow",
                                "snowing", "cold", "hot", "windy", "foggy", "weather", "clima", "lluvia"})
_ACK_TOKENS = frozenset({"thanks", "thank", "sure", "great", "okay", "ok", "yes", "yeah", "no", "problem", "perfect", "sounds",
                         "good", "right", "absolutely", "certainly", "gracias", "claro", "vale", "bien", "muy", "excelente",
                         "perfecto", "si", "bueno", "you", "welcome", "will", "do", "of", "course", "got", "it", "understood"})
_BUSINESS_TOKENS = frozenset({
    "fund", "funds", "etf", "class", "shares", "share", "allocation", "allocate", "fee", "fees", "expense", "expenses",
    "performance", "return", "returns", "risk", "volatility", "benchmark", "portfolio", "mandate", "client", "clients",
    "committee", "duration", "yield", "credit", "bond", "bonds", "equity", "equities", "income", "growth", "esg", "factsheet",
    "prospectus", "proposal", "platform", "approval", "diligence", "rfp", "ddq", "send", "schedule", "follow", "deck",
    "presentation", "tax", "bps", "invest", "investment", "investing", "redemption", "redemptions", "liquidity", "rebalance",
    "rebalancing", "model", "strategy", "manager", "research", "market", "markets", "rates", "rate", "inflation", "fondo",
    "fondos", "clase", "acciones", "bonos", "cartera", "comisiones", "rendimiento", "riesgo", "inversion", "mandato",
    "contoso", "woodgrove", "advisor", "adviser", "custodian", "statement", "ticket", "distribution", "dividend",
    "conversion", "convert", "basis", "points", "compare", "comparison", "factsheets", "ucits", "sma", "account",
    "enviar", "programar", "reunion", "llamada", "propuesta", "ficha", "tecnica", "estado", "cuenta",
})

# ---- relationship facts ---------------------------------------------------------------------------------------------
_TEAMS: dict[str, str] = {
    "seahawks": "Seattle Seahawks", "49ers": "San Francisco 49ers", "niners": "San Francisco 49ers", "cowboys": "Dallas Cowboys",
    "patriots": "New England Patriots", "packers": "Green Bay Packers", "chiefs": "Kansas City Chiefs",
    "eagles": "Philadelphia Eagles", "steelers": "Pittsburgh Steelers", "broncos": "Denver Broncos", "raiders": "Las Vegas Raiders",
    "chargers": "Los Angeles Chargers", "dolphins": "Miami Dolphins", "bills": "Buffalo Bills", "ravens": "Baltimore Ravens",
    "bengals": "Cincinnati Bengals", "browns": "Cleveland Browns", "colts": "Indianapolis Colts", "texans": "Houston Texans",
    "titans": "Tennessee Titans", "jaguars": "Jacksonville Jaguars", "vikings": "Minnesota Vikings", "saints": "New Orleans Saints",
    "falcons": "Atlanta Falcons", "buccaneers": "Tampa Bay Buccaneers", "commanders": "Washington Commanders",
    "lakers": "Los Angeles Lakers", "celtics": "Boston Celtics", "warriors": "Golden State Warriors", "knicks": "New York Knicks",
    "mavericks": "Dallas Mavericks", "nuggets": "Denver Nuggets", "blazers": "Portland Trail Blazers", "sonics": "Seattle SuperSonics",
    "raptors": "Toronto Raptors", "bucks": "Milwaukee Bucks", "yankees": "New York Yankees", "red sox": "Boston Red Sox",
    "dodgers": "Los Angeles Dodgers", "cubs": "Chicago Cubs", "mariners": "Seattle Mariners", "astros": "Houston Astros",
    "braves": "Atlanta Braves", "mets": "New York Mets", "phillies": "Philadelphia Phillies", "padres": "San Diego Padres",
    "blue jays": "Toronto Blue Jays", "orioles": "Baltimore Orioles", "kraken": "Seattle Kraken", "canucks": "Vancouver Canucks",
    "bruins": "Boston Bruins", "penguins": "Pittsburgh Penguins", "maple leafs": "Toronto Maple Leafs", "canadiens": "Montreal Canadiens",
    "oilers": "Edmonton Oilers", "blackhawks": "Chicago Blackhawks", "red wings": "Detroit Red Wings", "sounders": "Seattle Sounders",
    "arsenal": "Arsenal", "chelsea": "Chelsea", "liverpool": "Liverpool", "tottenham": "Tottenham Hotspur",
    "manchester united": "Manchester United", "man united": "Manchester United", "manchester city": "Manchester City",
    "real madrid": "Real Madrid", "barcelona": "FC Barcelona", "bayern": "Bayern Munich",
    "heat": "Miami Heat", "giants": "the Giants", "jets": "New York Jets", "bears": "Chicago Bears", "lions": "Detroit Lions",
    "rams": "Los Angeles Rams", "panthers": "Carolina Panthers", "cardinals": "Arizona Cardinals", "rangers": "the Rangers",
    "hawks": "Atlanta Hawks", "nets": "Brooklyn Nets", "bulls": "Chicago Bulls", "spurs": "San Antonio Spurs", "suns": "Phoenix Suns",
    "thunder": "Oklahoma City Thunder", "rockets": "Houston Rockets", "kings": "Sacramento Kings", "magic": "Orlando Magic",
}
_TEAM_AMBIGUOUS = frozenset({"heat", "giants", "jets", "bears", "lions", "rams", "panthers", "cardinals", "rangers", "hawks", "nets",
                             "bulls", "spurs", "suns", "thunder", "rockets", "kings", "magic", "saints", "bills", "browns",
                             "titans", "raiders", "chargers", "eagles", "warriors", "braves", "cubs", "bucks", "celtics"})
_FAN_CUE = re.compile(r"\b(fans?|supporters?|root(?:ing)? for|season[- ]tickets?|diehard|die-hard|lifelong|aficionado)\b", re.I)
_TEAM_ALT = "|".join(sorted((re.escape(t) for t in _TEAMS), key=len, reverse=True))
_TEAM_RE = re.compile(r"\b(" + _TEAM_ALT + r")\b", re.I)
_GO_TEAM_RE = re.compile(r"\bgo,?\s+(?:the\s+)?(?:" + _TEAM_ALT + r")\b", re.I)
_HOBBY_WORDS = (r"golf|fishing|hiking|skiing|snowboarding|sailing|cycling|biking|running|marathons?|triathlons?|tennis|pickleball|wine|"
                r"cooking|gardening|photography|travell?ing|reading|chess|baseball|basketball|soccer|football|hockey|cricket|rugby|"
                r"surfing|kayaking|camping|woodworking|golfing")
_HOBBY_RE = re.compile(
    r"\b(?:i|we)\s+(?:really\s+)?(?:love|enjoy|like|play|do|am into|are into|got into|have been into|coach(?:es)?|"
    r"train(?:ing)? for)\s+(?:to\s+)?(?:(?:my|our|the|a)\s+)?(?:(?:daughter|son|kids?|youth)(?:'s)?\s+)?(" + _HOBBY_WORDS + r")\b", re.I)
_HOBBY_MY_RE = re.compile(r"\b(?:my|our)\s+(?:[\w'-]+\s+){0,2}(" + _HOBBY_WORDS + r")\b", re.I)
_HOBBY_GO_RE = re.compile(r"\b(?:i|we)(?:'m|'ve|\s+am|\s+have)?\s+(?:been\s+)?(?:go(?:ing)?|went|gone|getting out|got out)\s+(" + _HOBBY_WORDS + r")\b", re.I)
_HOBBY_ALT_RE = re.compile(r"\b(?:on (?:the )?weekends?|in my (?:free|spare) time)\s*,?\s*(?:i|we)\s+(\w+(?:ing)?)", re.I)
_HOME_RE = re.compile(r"\b(?:grew up in|originally from|born and raised in|raised in)\s+((?:[A-Z][\w.'-]+\s?){1,3})")
_CONTACT_PREF_RE = re.compile(r"\b(?:i|we)\s+(?:really\s+)?prefer\s+(email|e-mail|phone|calls?|text(?:s| messages)?|teams|video|in person|"
                              r"short emails?)\b|\bbest (?:way|time) to (?:reach|contact) me (?:is|would be)\s+([^.,;]{3,40})", re.I)


def _label_of(role: SpeakerRole) -> SpeakerRole:
    return role if isinstance(role, SpeakerRole) else SpeakerRole.unknown


class RulesNotesPolicyEngine:
    """Implements engines.contracts.NotesPolicyEngine."""

    def apply(self, conv: CanonicalConversation, *, notes: NotesPolicy,
              pii_spans: dict[int, list[PiiSpan]]) -> NotesPolicyResult:
        omit = set(notes.omit_categories)
        redacted: list[Segment] = []
        out: list[PolicySegment] = []
        pii_ids: dict[str, list[int]] = defaultdict(list)
        sensitive_ids: dict[str, list[int]] = defaultdict(list)
        small_ids: list[int] = []
        facts: list[RelationshipFact] = []
        fact_index: dict[str, RelationshipFact] = {}
        actions: list[PolicyAction] = []

        for seg in conv.segments:
            role = self._role(conv, seg)
            text = seg.text
            sensitive = self._sensitive_category(text)
            if sensitive and sensitive in omit:
                sensitive_ids[sensitive].append(seg.id)
                out.append(PolicySegment(id=seg.id, speaker_id=seg.speaker_id, speaker_role=role, text="", label="sensitive",
                                         locale=seg.locale))
                redacted.append(seg.model_copy(update={"text": f"[omitted: {sensitive}]"}))
                continue
            text, categories = self._redact(text, [s for s in pii_spans.get(seg.id, []) if s.category in omit])
            for cat in categories:
                pii_ids[cat].append(seg.id)
            label: SegmentLabel = "business"
            segment_facts = (self._relationship(seg.text)
                             if notes.keep_relationship_context and role == SpeakerRole.customer else [])
            business = self._has_business_cue(text)
            if segment_facts:
                for fact_text, category in segment_facts:
                    key = re.sub(r"^(?:Supports|Follows) ", "", fact_text).lower()
                    known = fact_index.get(key)
                    if known is not None:
                        if fact_text.startswith("Supports ") and known.text.startswith("Follows "):
                            known.text = fact_text                       # a fan cue elsewhere upgrades "Follows" to "Supports"
                        continue
                    fact = RelationshipFact(text=fact_text, category=category,
                                            evidence=[Evidence(segment_ids=[seg.id], quote=self._quote(text, fact_text))])
                    fact_index[key] = fact
                    facts.append(fact)
                if not business:
                    label = "relationship"
            elif notes.drop_small_talk and self._is_small_talk(text, business):
                label = "small_talk"
                small_ids.append(seg.id)
            out.append(PolicySegment(id=seg.id, speaker_id=seg.speaker_id, speaker_role=role, text=text, label=label,
                                     locale=seg.locale))
            redacted.append(seg.model_copy(update={"text": text}))

        facts = self._merge_nicknames(facts)
        for fact in facts:
            actions.append(PolicyAction(kind="kept_relationship", category=fact.category,
                                        segment_ids=sorted({i for e in fact.evidence for i in e.segment_ids}),
                                        detail=f"Kept a non-sensitive rapport fact ({fact.category.replace('_', ' ')})"))
        for cat, ids in pii_ids.items():
            actions.append(PolicyAction(kind="pii_redacted", category=cat, segment_ids=sorted(set(ids)),
                                        detail=f"{len(ids)} {cat.replace('_', ' ')} item(s) replaced with a placeholder"))
        for cat, ids in sensitive_ids.items():
            actions.append(PolicyAction(kind="omitted_sensitive", category=cat, segment_ids=ids,
                                        detail=f"{len(ids)} segment(s) disclosing {cat.replace('_', ' ')} information omitted"))
        if small_ids:
            actions.append(PolicyAction(kind="dropped_small_talk", category="small_talk", segment_ids=small_ids,
                                        detail=f"{len(small_ids)} small-talk segment(s) excluded from the note"))
        return NotesPolicyResult(segments=out, actions=actions, relationship_facts=facts,
                                 redacted_conversation=conv.model_copy(update={"segments": redacted}))

    @staticmethod
    def _merge_nicknames(facts: list[RelationshipFact]) -> list[RelationshipFact]:
        """'go Hawks' after 'Seahawks': the short nickname is the same team, so keep only the full name."""
        def last(f: RelationshipFact) -> str:
            return f.text.split()[-1].lower()

        sports = [f for f in facts if f.category == "sports"]
        shadowed = {id(a) for a in sports for b in sports if a is not b and last(a) != last(b) and last(b).endswith(last(a))}
        return [f for f in facts if id(f) not in shadowed]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _role(conv: CanonicalConversation, seg: Segment) -> SpeakerRole:
        person = conv.participant(seg.speaker_id)
        if person is not None and person.role != SpeakerRole.unknown:
            return _label_of(person.role)
        return SpeakerRole.seller if seg.speaker_id == conv.meta.seller.id else SpeakerRole.customer

    @staticmethod
    def _sensitive_category(text: str) -> str | None:
        words = set(norm_words(text))
        norm = norm_text(text)
        strong = bool(words & _MEDICAL_STRONG)
        weak = bool(words & _MEDICAL_WEAK) or any(contains_phrase(norm, p) for p in (norm_text(x) for x in _MEDICAL_PHRASES))
        if not strong and not (weak and words & _PERSONAL_CONTEXT):
            return None
        return "family_health" if words & _FAMILY else "medical"

    @staticmethod
    def _redact(text: str, spans: list[PiiSpan]) -> tuple[str, list[str]]:
        if not spans:
            return text, []
        merged: list[list] = []
        for s in sorted(spans, key=lambda s: (s.start, -s.end)):
            if merged and s.start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], s.end)
            else:
                merged.append([s.start, s.end, s.category])
        for start, end, cat in reversed(merged):
            text = text[:start] + f"[{cat.upper()}]" + text[end:]
        return text, [m[2] for m in merged]

    @staticmethod
    def _has_business_cue(text: str) -> bool:
        return bool(set(norm_words(text)) & _BUSINESS_TOKENS)

    @staticmethod
    def _is_small_talk(text: str, business: bool) -> bool:
        if business:
            return False
        words = norm_words(text)
        if not words:
            return True
        norm = " ".join(words)
        if len(words) <= 6 and set(words) <= _ACK_TOKENS:
            return True
        return any(contains_phrase(norm, norm_text(p)) for p in _SMALL_TALK_PHRASES) or bool(set(words) & _SMALL_TALK_TOKENS)

    @staticmethod
    def _quote(text: str, fact_text: str) -> str:
        sents = sentences(text)
        key = fact_text.split()[-1].lower()
        return next((s for s in sents if key in s.lower()), sents[0] if sents else text)

    def _relationship(self, text: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        loyal = bool(_FAN_CUE.search(text) or _GO_TEAM_RE.search(text))        # a fan cue anywhere in the turn
        for sent in sentences(text):
            fan = loyal
            for m in _TEAM_RE.finditer(sent):
                key = m.group(1).lower()
                if key in _TEAM_AMBIGUOUS and not fan:
                    continue
                if key not in _TEAM_AMBIGUOUS and not (m.group(1)[:1].isupper() or fan):
                    continue
                team = _TEAMS[key]
                verb = "Supports" if fan else "Follows"
                found.append((f"{verb} {team if team.startswith('the ') else 'the ' + team}", "sports"))
                break
            hobby = _HOBBY_RE.search(sent) or _HOBBY_MY_RE.search(sent) or _HOBBY_GO_RE.search(sent)
            if hobby:
                found.append((f"Enjoys {hobby.group(1).lower()}", "hobby"))
            alt = _HOBBY_ALT_RE.search(sent)
            if alt and not hobby and alt.group(1).lower() not in {"have", "usually", "mostly", "try", "need", "get", "go", "am"}:
                found.append((f"Spends weekends {alt.group(1).lower()}", "hobby"))
            home = _HOME_RE.search(sent)
            if home:
                found.append((f"Grew up in {home.group(1).strip()}", "local"))
            pref = _CONTACT_PREF_RE.search(sent)
            if pref:
                what = (pref.group(1) or pref.group(2) or "").strip().lower()
                found.append((f"Prefers {what}" if pref.group(1) else f"Best reached {what}", "contact_preference"))
        return found

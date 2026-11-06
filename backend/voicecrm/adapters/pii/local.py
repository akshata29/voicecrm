"""Local (offline) PII detector: identifier PII only, with checksums, context cues and spoken-digit support.

Detects, per input text and with offsets relative to that text:

* ``payment_card``      Luhn-valid 13-19 digit runs (plain, spaced, dashed, or spoken: "four one one one ..." in
  English and Spanish), card security codes and expiry dates next to a card cue, and numbers split across two
  adjacent texts (a speaker interrupted mid-number); a Luhn failure next to an explicit "card number" cue is still
  flagged at lower confidence because speech recognition corrupts digits.
* ``bank_account``      IBAN (mod-97), ABA routing (checksum) and account numbers / sort codes / CLABE next to a cue
* ``government_id``     US SSN, UK National Insurance number, Spanish DNI/NIE (check letter), and passport or
  driver's licence numbers next to a cue
* ``credentials``       "my password is ...", "mi contraseña es ...", PIN / passcode phrases
* ``personal_contact``  email (typed and spoken "john dot smith at ..."), phone numbers, street addresses, PO boxes

Medical and family-health semantics are not detected here: the notes-policy engine owns them. English and Spanish
lexicons are always both active because calls code-switch; ``language`` is accepted for the port contract only.
Confidence reflects how strong the evidence is (checksum + cue > cue only > shape only); spans are never logged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from itertools import pairwise

from ...ports.pii import PII_CATEGORIES, PiiPort, PiiSpan
from .packing import text_records as _packed_records

logger = logging.getLogger(__name__)

_PRIORITY = {"payment_card": 0, "government_id": 1, "bank_account": 2, "credentials": 3, "personal_contact": 4}
assert set(_PRIORITY) <= set(PII_CATEGORIES)


@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    category: str
    confidence: float


# ---- checksums ------------------------------------------------------------------------------------------------------

def luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def aba_valid(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    d = [int(c) for c in digits]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def iban_valid(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if not 15 <= len(compact) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _dni_letter_ok(number: str, letter: str) -> bool:
    return _DNI_LETTERS[int(number) % 23] == letter.upper()


_IIN_RE = re.compile(
    r"(?:4|5[1-5]|2(?:2[2-9]|[3-6]\d|7[01])|3[47]|6(?:011|5|4[4-9]|22)|3(?:0[0-5]|[689])|35(?:2[89]|[3-8]))")


def _plausible_iin(digits: str) -> bool:
    return bool(_IIN_RE.match(digits))


# ---- spoken / typed digit runs --------------------------------------------------------------------------------------

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    "cero": "0", "uno": "1", "un": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
}
_REPEATERS = {"double": 2, "doble": 2, "triple": 3}
_TOKEN_RE = re.compile(r"\d+|[^\W\d_]+")
_GAP_RE = re.compile(r"[\s,.\-\u2013\u2014/()+]{0,4}")


@dataclass(frozen=True)
class _Tok:
    start: int
    end: int
    digits: str
    spoken: bool


@dataclass
class _Run:
    tokens: list[_Tok] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.tokens[0].start

    @property
    def end(self) -> int:
        return self.tokens[-1].end

    @property
    def digits(self) -> str:
        return "".join(t.digits for t in self.tokens)

    @property
    def spoken(self) -> bool:
        return any(t.spoken for t in self.tokens)


def _digit_runs(text: str) -> list[_Run]:
    """Maximal runs of digit tokens (typed groups or spoken digit words) separated only by light punctuation."""
    raw = list(_TOKEN_RE.finditer(text))
    runs: list[_Run] = []
    current: _Run | None = None
    i = 0
    while i < len(raw):
        m = raw[i]
        word = m.group(0)
        low = word.lower()
        tok: _Tok | None = None
        consumed = 1
        if word.isascii() and word.isdigit():
            tok = _Tok(m.start(), m.end(), word, False)
        elif low in _DIGIT_WORDS:
            tok = _Tok(m.start(), m.end(), _DIGIT_WORDS[low], True)
        elif low in _REPEATERS and i + 1 < len(raw):
            nxt = raw[i + 1]
            nxt_low = nxt.group(0).lower()
            nxt_text = nxt.group(0)
            digit = _DIGIT_WORDS.get(nxt_low) or (nxt_text if len(nxt_text) == 1 and nxt_text.isdigit() else None)
            if digit is not None and _GAP_RE.fullmatch(text[m.end():nxt.start()]):
                tok = _Tok(m.start(), nxt.end(), digit * _REPEATERS[low], True)
                consumed = 2
        if tok is None:
            current = None
        elif current is not None and _GAP_RE.fullmatch(text[current.end:tok.start]):
            current.tokens.append(tok)
        else:
            current = _Run([tok])
            runs.append(current)
        i += consumed
    return runs


# ---- context cues (anchored: the cue must sit right before the number) ----------------------------------------------

_GLUE = (r"(?:\s+(?:is|was|are|es|ser[a\u00e1]|number|no\.?|num|#|n[u\u00fa]mero|code|c[o\u00f3]digo|de|del|the|my|mi|"
         r"on|at|al))*"
         r"[\s:#=\-]*")


def _cue(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{pattern}){_GLUE}$", re.IGNORECASE)


_CUE_CARD = _cue(r"card|visa|master\s?card|amex|american express|discover|tarjeta|cr[e\u00e9]dito|d[e\u00e9]bito")
_CUE_CVV = _cue(r"cvv2?|cvc2?|csc|security code|card verification(?: code| value)?|c[o\u00f3]digo de seguridad|"
                r"c[o\u00f3]digo de verificaci[o\u00f3]n")
_CUE_SSN = _cue(r"social security|social|ssn|ss number|seguro social")
_CUE_ROUTING = _cue(r"routing|aba|transit|n[u\u00fa]mero de ruta")
_CUE_BANK = _cue(r"account|acct|iban|sort code|clabe|cuenta(?: bancaria| corriente| de ahorros)?")
_CUE_PHONE = _cue(r"phone|telephone|cell|mobile|whatsapp|call me|reach me|text me|contact me|my number|"
                  r"tel[e\u00e9]fono|celular|m[o\u00f3]vil|ll[a\u00e1]ma(?:me|nos)?|mi n[u\u00fa]mero")
_CUE_PIN = _cue(r"pin|passcode|access code|c[o\u00f3]digo de acceso|clave|contrase[n\u00f1]a|password")
_CARD_CUE_ANYWHERE = re.compile(
    r"\b(?:card|visa|master\s?card|amex|tarjeta|cr[e\u00e9]dito|d[e\u00e9]bito)\b", re.IGNORECASE)

_ADDRESS_SUFFIX = (r"(?i:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl|terrace|ter|"
                   r"highway|hwy|parkway|pkwy|circle|cir|square|sq)")
_ADDR_TAIL_RE = re.compile(rf"\s+(?!(?:AM|PM)\b)(?:[A-Z][A-Za-z.'\-]*\s+){{1,4}}{_ADDRESS_SUFFIX}\b\.?")


# ---- typed-format patterns ------------------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_TLDS = (r"com|net|org|edu|gov|mil|int|io|ai|app|dev|co|uk|es|mx|us|ca|de|fr|it|nl|ch|se|be|ie|au|nz|jp|cn|in|br|ar|cl|pe|"
         r"info|biz|example|test|invalid|local")
_SPOKEN_EMAIL_RE = re.compile(
    r"\b(?P<local>[A-Za-z0-9]+(?:\s+(?:dot|punto|underscore|dash|guion)\s+[A-Za-z0-9]+)*)\s+(?:at|arroba)\s+"
    r"(?:[A-Za-z0-9\-]+(?:\.[A-Za-z]{2,})+"
    rf"|[A-Za-z0-9\-]+(?:\s+(?:dot|punto|dash|guion)\s+[A-Za-z0-9\-]+)*\s+(?:dot|punto)\s+(?:{_TLDS}))\b",
    re.IGNORECASE)
_EMAIL_CUE_RE = re.compile(r"\b(?:e-?mail|mail|correo|address|direcci[o\u00f3]n)\b", re.IGNORECASE)
_EMAIL_JOINER_RE = re.compile(r"\b(?:dot|punto|underscore|dash|guion)\b|\d", re.IGNORECASE)
_PHONE_RES = (
    re.compile(r"(?<![\w.])(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\w])"),     # US / Canada
    re.compile(r"(?<![\w.])\+\d{1,3}[\s.\-]?(?:\(?\d{1,4}\)?[\s.\-]?){2,5}\d(?![\w])"),               # international
    re.compile(r"(?<![\w.])0\d{2,4}\s\d{3,4}\s?\d{3,4}(?![\w])"),                                      # UK
    re.compile(r"(?<![\w.])[6-9]\d{2}[\s.\-]\d{2}[\s.\-]\d{2}[\s.\-]\d{2}(?![\w])"),                   # Spain
    re.compile(r"(?<![\w.])\d{2}[\s.\-](?!(?:19|20)\d{2}\b)\d{4}[\s.\-]\d{4}(?![\w])"),                # Mexico
)
_STREET_RE = re.compile(
    rf"\b\d{{1,6}}\s+(?:[NSEW]\.?\s+)?(?!(?:AM|PM)\b)(?:[A-Z][A-Za-z.'\-]*\s+){{1,4}}{_ADDRESS_SUFFIX}\b\.?"
    r"(?:,?\s+(?i:apt|apartment|suite|ste|unit)\.?\s*#?\s*[\w\-]+)?"
    r"(?:,\s*[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)*,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?)?")
_STREET_ES_RE = re.compile(
    r"\b(?i:calle|c/|avenida|avda\.?|av\.|paseo|plaza|carretera|camino|ronda|bulevar|pasaje|traves[i\u00ed]a)\s+"
    r"(?:(?i:de)\s+(?:(?i:la|los|las|el)\s+)?)?"
    r"[A-Z\u00c1\u00c9\u00cd\u00d3\u00da\u00d1][\w\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1.'\-]*"
    r"(?:\s+[A-Z\u00c1\u00c9\u00cd\u00d3\u00da\u00d1][\w\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1.'\-]*){0,3}"
    r"\s*(?:,|#|(?i:n[u\u00fa]mero|n[\u00ba\u00b0o]\.?|num\.?))?\s*\d{1,5}\b")
_POBOX_RE = re.compile(r"\bP\.?\s?O\.?\s+Box\s+\d+\b|\bapartado de correos\s+\d+\b", re.IGNORECASE)
_SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
_UK_NI_RE = re.compile(r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z](?:\s?\d{2}){3}\s?[A-D]\b")
_DNI_RE = re.compile(r"(?<![\w-])(\d{8})[- ]?([A-HJ-NP-TV-Z])(?![\w])", re.IGNORECASE)
_NIE_RE = re.compile(r"(?<![\w-])([XYZ])[- ]?(\d{7})[- ]?([A-HJ-NP-TV-Z])(?![\w])", re.IGNORECASE)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_SWIFT_RE = re.compile(
    r"\b(?:swift|bic)(?:\s+code)?\W{1,3}(?:is\W+)?([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", re.IGNORECASE)
_PASSPORT_RE = re.compile(
    r"\b(?:passport|pasaporte)(?:\s+(?:number|no\.?|num|n[u\u00fa]mero))?\s*(?:is|es|:|#)?\s*([A-Z0-9]{6,9})\b",
    re.IGNORECASE)
_LICENSE_RE = re.compile(
    r"\b(?:driver'?s?\s+licen[sc]e|licencia de conducir|carnet de conducir)(?:\s+(?:number|no\.?|num|n[u\u00fa]mero))?"
    r"\s*(?:is|es|:|#)?\s*([A-Z0-9][A-Z0-9\-]{5,15})\b", re.IGNORECASE)
_EXPIRY_RE = re.compile(
    r"\b(?:exp(?:ires|iry|iration)?|vence|vencimiento|caduca)\b\W+(?:date\W+)?(?:(?:is|es)\W+)?"
    r"(\d{1,2}\s*[/\-]\s*\d{2,4})\b", re.IGNORECASE)

_CRED_CUE = (r"password|passcode|pass\s?phrase|login code|security answer|secret(?: key| code| word)?|api key|access code|"
             r"one[- ]time (?:code|password)|otp|contrase[n\u00f1]a|clave(?: de acceso| secreta)?|c[o\u00f3]digo de acceso|"
             r"c[o\u00f3]digo secreto|palabra clave|pin(?: number| code)?")
_CRED_RE = re.compile(
    rf"\b(?:{_CRED_CUE})\s*(?:is|was|will be|would be|es|era|ser[a\u00e1]|=|:)\s*"
    r"[\"'\u201c\u2018]?(?P<val>\S+(?:\s+\S+){0,7})",
    re.IGNORECASE)
_NOT_A_SECRET = frozenset({
    "required", "needed", "expired", "reset", "incorrect", "correct", "wrong", "invalid", "not", "too", "a", "an", "the",
    "something", "long", "strong", "weak", "secure", "important", "fine", "okay", "ok", "sent", "here", "there", "this",
    "that", "it", "default", "changed", "updated", "necesaria", "necesario", "incorrecta", "incorrecto", "correcta",
    "correcto", "muy", "un", "una", "el", "la"})
_STOP_WORDS = frozenset({
    "and", "but", "so", "then", "because", "or", "also", "which", "when", "if", "for", "to", "please", "okay", "ok",
    "thanks", "y", "pero", "que", "entonces", "o", "por", "favor", "gracias"})


# ---- detector -------------------------------------------------------------------------------------------------------

class LocalPiiDetector(PiiPort):
    name = "local"

    def text_records(self, texts: list[str]) -> int:
        """Text records Azure AI Language would bill for these texts (with default packing); used for cost projections."""
        return _packed_records(texts)

    async def detect_many(self, texts: list[str], *, language: str) -> list[list[PiiSpan]]:
        scans = [self._scan(text) for text in texts]
        hits = [list(h) for h, _ in scans]
        for i in range(len(texts) - 1):
            self._link_split_number(texts, scans, hits, i)
        return [self._to_spans(text, found) for text, found in zip(texts, hits, strict=True)]

    def detect(self, text: str, *, language: str = "en") -> list[PiiSpan]:
        """Synchronous single-text variant (same spans as ``detect_many([text])[0]``)."""
        hits, _ = self._scan(text)
        return self._to_spans(text, hits)

    # ---- scanning ---------------------------------------------------------------------------------------------
    def _scan(self, text: str) -> tuple[list[_Hit], list[_Run]]:
        if not text or not text.strip():
            return [], []
        runs = _digit_runs(text)
        hits: list[_Hit] = []
        for run in runs:
            hits.extend(self._classify_run(text, run))
        hits.extend(_pattern_hits(text))
        return hits, runs

    def _classify_run(self, text: str, run: _Run) -> list[_Hit]:
        digits = run.digits
        n = len(digits)
        if _is_thousands_grouped(text, run) and n < 13:
            return []                                           # "1,250,000" is an amount, not an identifier
        before = text[max(0, run.start - 90):run.start]
        if 13 <= n <= 19 and luhn_valid(digits):
            return [_Hit(run.start, run.end, "payment_card", 0.95 if _plausible_iin(digits) else 0.7)]
        if n >= 13:
            windows = _card_windows(run, before_cue=bool(_CUE_CARD.search(before)))
            if windows:
                return windows
        if (n == 16 and _plausible_iin(digits)) or (n == 15 and digits[:2] in ("34", "37")):
            return [_Hit(run.start, run.end, "payment_card", 0.5)]      # card-shaped but failing Luhn: likely ASR/typo
        if 3 <= n <= 4 and _CUE_CVV.search(before):
            return [_Hit(run.start, run.end, "payment_card", 0.9)]
        if n == 9 and _CUE_SSN.search(before):
            return [_Hit(run.start, run.end, "government_id", 0.9)]
        if n == 9 and _CUE_ROUTING.search(before):
            return [_Hit(run.start, run.end, "bank_account", 0.9 if aba_valid(digits) else 0.7)]
        if 13 <= n <= 19 and _CUE_CARD.search(before):
            return [_Hit(run.start, run.end, "payment_card", 0.6)]
        if 6 <= n <= 17 and _CUE_BANK.search(before):
            return [_Hit(run.start, run.end, "bank_account", 0.75)]
        if n in (10, 11) and _CUE_PHONE.search(before):
            return [_Hit(run.start, run.end, "personal_contact", 0.85)]
        if n in (10, 11) and run.spoken:
            return [_Hit(run.start, run.end, "personal_contact", 0.55)]
        if 3 <= n <= 8 and _CUE_PIN.search(before):
            return [_Hit(run.start, run.end, "credentials", 0.8)]
        if n <= 6:
            tail = _ADDR_TAIL_RE.match(text, run.end)
            if tail:                                         # "one two three Main Street"
                return [_Hit(run.start, tail.end(), "personal_contact", 0.85)]
        return []

    def _link_split_number(
        self, texts: list[str], scans: list[tuple[list[_Hit], list[_Run]]], hits: list[list[_Hit]], i: int,
    ) -> None:
        """A card number interrupted between two adjacent texts: flag both halves when together they pass Luhn."""
        (_, runs_a), (_, runs_b) = scans[i], scans[i + 1]
        if not runs_a or not runs_b:
            return
        tail, head = runs_a[-1], runs_b[0]
        if texts[i][tail.end:].strip(" \t\r\n.,;:!?-") or texts[i + 1][:head.start].strip(" \t\r\n.,;:!?-"):
            return                                           # the digits do not end / begin the texts
        if any(h.category == "payment_card" and h.start <= tail.start < h.end for h in hits[i]):
            return
        combined = tail.digits + head.digits
        if not (13 <= len(combined) <= 19 and luhn_valid(combined)):
            return
        before = texts[i][max(0, tail.start - 90):tail.start]
        if not (_plausible_iin(combined) or _CUE_CARD.search(before)):
            return
        hits[i].append(_Hit(tail.start, tail.end, "payment_card", 0.8))
        hits[i + 1].append(_Hit(head.start, head.end, "payment_card", 0.8))

    # ---- output -----------------------------------------------------------------------------------------------
    @staticmethod
    def _to_spans(text: str, hits: list[_Hit]) -> list[PiiSpan]:
        return [PiiSpan(start=h.start, end=h.end, category=h.category, text=text[h.start:h.end],
                        confidence=round(h.confidence, 2), source="local") for h in _resolve(hits)]


_MAX_WINDOW_TOKENS = 120          # a longer run is a list of numbers, not a spoken card; bounds the window search


def _is_thousands_grouped(text: str, run: _Run) -> bool:
    """Typed amounts such as 1,250,000: comma-separated groups of exactly three digits after a 1-3 digit head."""
    tokens = run.tokens
    if len(tokens) < 2 or any(t.spoken for t in tokens) or not 1 <= len(tokens[0].digits) <= 3:
        return False
    return all(len(t.digits) == 3 for t in tokens[1:]) and all(
        text[a.end:b.start] == "," for a, b in pairwise(tokens))


def _card_windows(run: _Run, *, before_cue: bool) -> list[_Hit]:
    """Card-shaped windows inside a long run (e.g. a card number followed by its expiry digits)."""
    tokens = run.tokens[:_MAX_WINDOW_TOKENS]
    out: list[_Hit] = []
    i = 0
    while i < len(tokens):
        total = ""
        best: int | None = None
        for j in range(i, len(tokens)):
            total += tokens[j].digits
            if len(total) > 19:
                break
            if len(total) >= 13 and luhn_valid(total) and (_plausible_iin(total) or (before_cue and i == 0)):
                best = j
        if best is None:
            i += 1
            continue
        out.append(_Hit(tokens[i].start, tokens[best].end, "payment_card", 0.9))
        i = best + 1
    return out


def _pattern_hits(text: str) -> list[_Hit]:
    hits: list[_Hit] = []

    def add(m: re.Match[str], category: str, confidence: float, group: int = 0) -> None:
        hits.append(_Hit(m.start(group), m.end(group), category, confidence))

    for m in _EMAIL_RE.finditer(text):
        add(m, "personal_contact", 0.95)
    for m in _SPOKEN_EMAIL_RE.finditer(text):
        # "looking at amazon dot com" is not an address: need a joiner/digit in the local part, or an email cue before it
        if _EMAIL_JOINER_RE.search(m.group("local")) or _EMAIL_CUE_RE.search(text[max(0, m.start() - 60):m.start()]):
            add(m, "personal_contact", 0.85)
    for pattern in _PHONE_RES:
        for m in pattern.finditer(text):
            if 8 <= sum(c.isdigit() for c in m.group(0)) <= 15:
                add(m, "personal_contact", 0.85)
    for pattern in (_STREET_RE, _STREET_ES_RE, _POBOX_RE):
        for m in pattern.finditer(text):
            add(m, "personal_contact", 0.85)
    for m in _SSN_RE.finditer(text):
        add(m, "government_id", 0.85 if m.group(1) == "-" else 0.7)
    for m in _UK_NI_RE.finditer(text):
        add(m, "government_id", 0.9)
    for m in _DNI_RE.finditer(text):
        if _dni_letter_ok(m.group(1), m.group(2)):
            add(m, "government_id", 0.9)
    for m in _NIE_RE.finditer(text):
        if _dni_letter_ok(str("XYZ".index(m.group(1).upper())) + m.group(2), m.group(3)):
            add(m, "government_id", 0.9)
    for m in _IBAN_RE.finditer(text):
        if iban_valid(m.group(0)):
            add(m, "bank_account", 0.95)
    for m in _SWIFT_RE.finditer(text):
        add(m, "bank_account", 0.8, group=1)
    for pattern in (_PASSPORT_RE, _LICENSE_RE):
        for m in pattern.finditer(text):
            if any(c.isdigit() for c in m.group(1)):
                add(m, "government_id", 0.8, group=1)
    for m in _EXPIRY_RE.finditer(text):
        if _CARD_CUE_ANYWHERE.search(text[max(0, m.start() - 100):m.start()]):
            add(m, "payment_card", 0.7, group=1)
    for m in _CRED_RE.finditer(text):
        span = _credential_span(text, m)
        if span:
            hits.append(_Hit(span[0], span[1], "credentials", 0.9))
    return hits


_CLOSING_QUOTE = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def _credential_span(text: str, m: re.Match[str]) -> tuple[int, int] | None:
    """The secret after "password is": a quoted string, else up to 4 words (8 for digit sequences) cut at clause ends."""
    value, base = m.group("val"), m.start("val")
    opening = text[base - 1] if base else ""
    if opening in _CLOSING_QUOTE:                              # password is "blue horse!" -> everything inside the quotes
        close = value.find(_CLOSING_QUOTE[opening])
        if close > 0:
            return base, base + close
    words = list(re.finditer(r"\S+", value))
    first = words[0].group(0).strip(".,;:!?\"'\u201c\u201d\u2018\u2019)").lower()
    if first in _NOT_A_SECRET:
        return None
    limit = 8 if first.isdigit() or first in _DIGIT_WORDS else 4
    end = None
    for index, word in enumerate(words[:limit]):
        token = word.group(0)
        low = token.strip(".,;:!?\"'\u201c\u201d\u2018\u2019)").lower()
        if index and low in _STOP_WORDS:
            break
        end = word.end()
        if token[-1] in ".,;:!?":
            break
    if end is None:
        return None
    start, stop = base, base + end
    while stop > start and value[stop - base - 1] in ".,;:!?\"'\u201d\u2019)":
        stop -= 1
    return (start, stop) if stop > start else None


def _resolve(hits: list[_Hit]) -> list[_Hit]:
    """Non-overlapping hits: strongest evidence wins; overlapping hits of one category are unioned."""
    ordered = sorted(hits, key=lambda h: (-h.confidence, -(h.end - h.start), _PRIORITY[h.category], h.start))
    kept: list[_Hit] = []
    for hit in ordered:
        clash = next((k for k in kept if hit.start < k.end and k.start < hit.end), None)
        if clash is None:
            kept.append(hit)
        elif clash.category == hit.category:
            kept[kept.index(clash)] = _Hit(min(clash.start, hit.start), max(clash.end, hit.end), clash.category,
                                           clash.confidence)
    merged: list[_Hit] = []
    for hit in sorted(kept, key=lambda h: h.start):
        if merged and hit.start < merged[-1].end:            # a union may now touch a neighbour of the same category
            previous = merged[-1]
            if previous.category == hit.category:
                merged[-1] = _Hit(previous.start, max(previous.end, hit.end), previous.category, previous.confidence)
            continue
        merged.append(hit)
    return merged

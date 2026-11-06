"""Deterministic, realistic ASR mistakes for stress-testing product recognition.

Used by the mock transcriber (``TranscriptionRequest.asr_noise``) and by the pipeline on platform transcripts. The
mistakes mimic what speech recognisers do to fund names: spoken share-class letters ("class C" -> "class see"),
"Contoso" -> "Contosa", Fund <-> Funds, dropped articles, number words <-> digits and homophones.

Only ``Segment.text`` is ever touched; ids, speakers, timings, locales and confidences are preserved. For a given
``seed`` the output is identical across runs and platforms, and ``level=0`` returns the input unchanged.
"""
from __future__ import annotations

import random
import re
from collections.abc import Callable

from ...domain.conversation import Segment

# Per-rule probabilities at level=1.0; the effective probability of a rule is ``level * weight``.
_W_SHARE_CLASS = 1.0
_W_BRAND = 0.35
_W_PLURAL = 0.5
_W_ARTICLE = 0.2
_W_NUM_WORD_TO_DIGIT = 0.4
_W_NUM_DIGIT_TO_WORD = 0.3
_W_HOMOPHONE = 0.15

_Chooser = Callable[[re.Match[str], random.Random], str]


def apply_asr_noise(segments: list[Segment], *, seed: int = 7, level: float = 0.5) -> list[Segment]:
    """Returns copies of ``segments`` with injected ASR-style text errors (``level`` in 0..1 scales the error rate)."""
    level = min(1.0, max(0.0, float(level)))
    if level == 0.0:
        return [s.model_copy() for s in segments]
    out: list[Segment] = []
    for seg in segments:
        rng = random.Random(f"{seed}:{seg.id}")           # per-segment stream: independent of the other segments
        out.append(seg.model_copy(update={"text": _noisy(seg.text, rng, level)}))
    return out


def _noisy(text: str, rng: random.Random, level: float) -> str:
    # Share-class phrases go last so later rules cannot undo (or drop the letter of) a deliberate class error.
    text = _drop_articles(text, rng, level * _W_ARTICLE)
    text = _flip_fund_number(text, rng, level * _W_PLURAL)
    text = _numbers(text, rng, level)
    text = _homophones(text, rng, level * _W_HOMOPHONE)
    text = _brand_names(text, rng, level * _W_BRAND)
    return _share_class(text, rng, level * _W_SHARE_CLASS)


def _sub(pattern: re.Pattern[str], text: str, rng: random.Random, p: float, choose: _Chooser) -> str:
    def repl(match: re.Match[str]) -> str:
        roll = rng.random()                               # always consume one draw so streams stay aligned
        return choose(match, rng) if roll < p else match.group(0)

    return pattern.sub(repl, text)


def _match_case(source: str, replacement: str) -> str:
    if len(source) > 1 and source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


# ---- share classes ------------------------------------------------------------------------------------------------

_CLASS_RE = re.compile(r"\b(?P<kw>class|clase)\s+(?P<cls>r\s?6|advisor|adviser|asesor|[acir])\b", re.IGNORECASE)
_SHARES_RE = re.compile(r"\b(?P<cls>[CR])\s+(?P<tail>shares?)\b")       # not "I": "I share ..." is ordinary speech

# {kw} is the original "class"/"clase" word; reordered variants ("c class") mimic speakers that say the letter first.
_CLASS_VARIANTS_EN: dict[str, list[str]] = {
    "c": ["{kw} see", "{kw} sea", "c {kw}"],
    "a": ["{kw} eh", "a {kw}"],
    "i": ["{kw} eye", "i {kw}"],
    "r": ["{kw} are", "r {kw}"],
    "r6": ["{kw} r six", "{kw} are six", "r6 {kw}"],
    "advisor": ["{kw} adviser", "adviser {kw}"],
    "adviser": ["{kw} advisor"],
}
_CLASS_VARIANTS_ES: dict[str, list[str]] = {
    "c": ["{kw} ce", "{kw} se", "ce {kw}"],
    "a": ["{kw} ah"],
    "r6": ["{kw} erre seis"],
    "advisor": ["{kw} asesor"],
    "asesor": ["{kw} advisor"],
}
_SHARES_VARIANTS = {"C": ["see", "sea"], "R": ["are"]}


def _share_class(text: str, rng: random.Random, p: float) -> str:
    def choose(m: re.Match[str], r: random.Random) -> str:
        kw = m.group("kw")
        key = re.sub(r"\s+", "", m.group("cls").lower())
        table = _CLASS_VARIANTS_ES if kw.lower() == "clase" else _CLASS_VARIANTS_EN
        variants = table.get(key)
        if not variants:
            return m.group(0)
        out = r.choice(variants).format(kw=kw)
        return _match_case(kw, out) if kw[:1].isupper() else out

    text = _sub(_CLASS_RE, text, rng, p, choose)

    def choose_shares(m: re.Match[str], r: random.Random) -> str:
        return f"{r.choice(_SHARES_VARIANTS[m.group('cls')])} {m.group('tail')}"

    return _sub(_SHARES_RE, text, rng, p, choose_shares)


# ---- brand and product words --------------------------------------------------------------------------------------

_BRAND_RE = re.compile(r"\b(?P<w>Contoso|Woodgrove)\b")
_BRAND_VARIANTS = {"contoso": ["Contosa", "Kontoso"], "woodgrove": ["Wood grove"]}


def _brand_names(text: str, rng: random.Random, p: float) -> str:
    return _sub(_BRAND_RE, text, rng, p, lambda m, r: r.choice(_BRAND_VARIANTS[m.group("w").lower()]))


_FUND_RE = re.compile(r"\b(?P<w>funds?|fondos?)\b", re.IGNORECASE)


def _flip_fund_number(text: str, rng: random.Random, p: float) -> str:
    def choose(m: re.Match[str], _r: random.Random) -> str:
        word = m.group("w")
        flipped = word[:-1] if word.lower().endswith("s") else word + "s"
        return _match_case(word, flipped) if word.isupper() else flipped

    return _sub(_FUND_RE, text, rng, p, choose)


# ---- articles -----------------------------------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"(?<!\S)(?P<art>the|an?|el|la|los|las|una?)\s+", re.IGNORECASE)
_CLASS_WORDS = {"class", "clase", "series", "serie"}


def _drop_articles(text: str, rng: random.Random, p: float) -> str:
    def repl(m: re.Match[str]) -> str:
        roll = rng.random()
        before = text[: m.start()].split()
        # "class A" must keep its letter: an "a" right after class/clase is a share class, not an article
        if before and before[-1].lower().strip(",.;:") in _CLASS_WORDS:
            return m.group(0)
        return "" if roll < p else m.group(0)

    return _ARTICLE_RE.sub(repl, text)


# ---- numbers ------------------------------------------------------------------------------------------------------

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "twelve": "12", "fifteen": "15", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "cero": "0", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
    "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
}
_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                "7": "seven", "8": "eight", "9": "nine", "10": "ten", "12": "twelve", "15": "fifteen", "20": "twenty"}
_NUMWORD_RE = re.compile(r"\b(?P<w>" + "|".join(_NUM_WORDS) + r")\b", re.IGNORECASE)
_DIGIT_RE = re.compile(r"(?<![\w.,:/$%-])(?P<d>\d{1,2})(?![\w.,:/%])")


def _numbers(text: str, rng: random.Random, level: float) -> str:
    text = _sub(_NUMWORD_RE, text, rng, level * _W_NUM_WORD_TO_DIGIT, lambda m, _r: _NUM_WORDS[m.group("w").lower()])
    return _sub(_DIGIT_RE, text, rng, level * _W_NUM_DIGIT_TO_WORD,
                lambda m, _r: _DIGIT_WORDS.get(m.group("d"), m.group(0)))


# ---- homophones and spelling variants -----------------------------------------------------------------------------

_HOMOPHONES = {
    "for": "four", "four": "for", "two": "to", "too": "to", "one": "won", "their": "there", "there": "their",
    "by": "buy", "fund": "fun", "equity": "equities", "advisor": "adviser", "adviser": "advisor",
}
_HOMOPHONE_RE = re.compile(r"\b(?P<w>" + "|".join(_HOMOPHONES) + r")\b", re.IGNORECASE)


def _homophones(text: str, rng: random.Random, p: float) -> str:
    return _sub(_HOMOPHONE_RE, text, rng, p, lambda m, _r: _match_case(m.group("w"), _HOMOPHONES[m.group("w").lower()]))

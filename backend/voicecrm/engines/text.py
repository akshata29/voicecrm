"""Text helpers shared by the rules engines: offset-preserving tokenising, accent folding, sentence splitting."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

_TOKEN_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*(?:\.[^\W_]+)*", re.UNICODE)       # keeps "U.S", "don't"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_ABBREVIATIONS = {"vs", "mr", "mrs", "ms", "dr", "e.g", "i.e", "etc", "inc", "co", "approx", "sr", "jr", "u.s"}
_SENTENCE_END_RE = re.compile(r"[.!?\u2026]+[\"')\]\u201d]*\s+|\n+")


@dataclass(frozen=True, slots=True)
class Tok:
    text: str          # as written
    norm: str          # accent-folded, lower-case, alphanumerics only
    start: int         # character offsets into the original text
    end: int


# The helpers below are pure and called repeatedly on the same strings (every rule re-reads every segment, and the lexicon
# phrases are normalised per segment), so results are memoised; the cached values are immutable and callers get copies.
@lru_cache(maxsize=65536)
def fold(text: str) -> str:
    """Lower-case, accent-folded, alphanumerics only (used per token, so character offsets stay valid)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return _NON_ALNUM_RE.sub("", text)


@lru_cache(maxsize=2048)
def _tokens(text: str) -> tuple[Tok, ...]:
    out: list[Tok] = []
    for m in _TOKEN_RE.finditer(text):
        norm = fold(m.group(0))
        if norm:
            out.append(Tok(m.group(0), norm, m.start(), m.end()))
    return tuple(out)


def tokenize(text: str) -> list[Tok]:
    return list(_tokens(text))


def normalize_surface(text: str) -> str:
    """Key used for learned aliases; identical to the pipeline's normalisation (lower, accent-fold, punctuation -> space)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()


@lru_cache(maxsize=2048)
def _norm_words(text: str) -> tuple[str, ...]:
    return tuple(t.norm for t in _tokens(text))


def norm_words(text: str) -> list[str]:
    return list(_norm_words(text))


@lru_cache(maxsize=8192)
def norm_text(text: str) -> str:
    """Normalised phrase used for lexicon matching: folded tokens joined by single spaces."""
    return " ".join(_norm_words(text))


def find_sequence(tokens: list[str], phrase: tuple[str, ...] | list[str], start: int = 0) -> int:
    """Index of the first occurrence of `phrase` in `tokens` at or after `start`, else -1."""
    n = len(phrase)
    if n == 0:
        return -1
    first = phrase[0]
    for i in range(start, len(tokens) - n + 1):
        if tokens[i] == first and tuple(tokens[i:i + n]) == tuple(phrase):
            return i
    return -1


def contains_phrase(norm: str, phrase: str) -> bool:
    """Whole-word containment on already-normalised strings ('fee comparison' in 'send the fee comparison')."""
    return f" {phrase} " in f" {norm} "


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence boundaries as (start, end) character offsets; tolerant of abbreviations such as 'U.S.' and 'vs.'."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        before = text[start:m.start()].split()
        last = before[-1].lower().strip("\"'([") if before else ""      # raw word: "U.S" keeps its dot, the pronoun "us" has none
        if m.group(0).startswith(".") and last in _ABBREVIATIONS and len(m.group(0).strip()) == 1:
            continue
        end = m.start() + len(m.group(0).rstrip())
        if text[start:end].strip():
            spans.append((start, end))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text.rstrip())))
    return spans or ([(0, len(text))] if text.strip() else [])


def sentences(text: str) -> list[str]:
    return [text[a:b].strip() for a, b in sentence_spans(text)]


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"

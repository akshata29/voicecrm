"""Tiny English/Spanish language guess for platform transcripts that do not say which language they are in.

Deliberately conservative: it answers only when one language clearly dominates by function-word counts, otherwise None
(the pipeline then falls back to the seller's language hint or the policy default). Audio goes through Azure Speech
language identification instead; this exists for uploaded VTT / JSON transcripts.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_EN = frozenset(
    "the and of to in is that for it with as was on are you we this have be at your our they will would can "
    "about from not but what which there their how okay yes thanks thank hello please".split()
)
_ES = frozenset(
    "el la los las de que y en un una es por con para no se su al lo como mas pero muy sus este esta son del "
    "nos le ya si hola gracias buenos dias favor claro perfecto tambien vamos tenemos podemos queremos".split()
)
_MIN_HITS = 8
_DOMINANCE = 2.0

LANGUAGE_CODES = {"en": "en-US", "es": "es-US"}


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.findall(r"[a-z]+", folded)


def detect_language(texts: Iterable[str]) -> str | None:
    """'en-US' / 'es-US' when one language clearly dominates, else None."""
    english = spanish = 0
    for text in texts:
        for token in _tokens(text):
            english += token in _EN
            spanish += token in _ES
    if max(english, spanish) < _MIN_HITS:
        return None
    if spanish >= _DOMINANCE * english:
        return LANGUAGE_CODES["es"]
    if english >= _DOMINANCE * spanish:
        return LANGUAGE_CODES["en"]
    return None

"""Text helpers: offset-preserving tokenising, accent folding, sentence splitting, surface normalisation."""
from __future__ import annotations

import pytest

from voicecrm.engines import text
from voicecrm.pipeline.service import normalize_surface as pipeline_normalize_surface


def test_tokenize_offsets_slice_back_to_the_original_text():
    original = "Contoso Equity Fund, Class C!  (see 'p. 4')"
    toks = text.tokenize(original)
    assert [t.norm for t in toks] == ["contoso", "equity", "fund", "class", "c", "see", "p", "4"]
    assert all(original[t.start:t.end] == t.text for t in toks)


def test_tokenize_folds_accents_but_keeps_the_written_form():
    toks = text.tokenize("La jubilación y las comisiones, mañana")
    assert [t.norm for t in toks] == ["la", "jubilacion", "y", "las", "comisiones", "manana"]
    assert toks[1].text == "jubilación"
    assert text.fold("Ünico-Fondo_2") == "unicofondo2"


def test_tokenize_keeps_abbreviations_and_contractions_together():
    toks = text.tokenize("the U.S. market, don't worry")
    assert [t.norm for t in toks] == ["the", "us", "market", "dont", "worry"]
    assert toks[1].text == "U.S"                                   # the trailing period is not part of the token


def test_tokenize_drops_pure_punctuation_and_underscores():
    assert text.tokenize("-- ... !!") == []
    assert [t.norm for t in text.tokenize("a_b")] == ["a", "b"]


def test_sentence_spans_do_not_split_on_us_or_vs_abbreviations():
    original = "The U.S. market rose. Funds vs. ETFs differ. Done!"
    spans = text.sentence_spans(original)
    assert [original[a:b] for a, b in spans] == ["The U.S. market rose.", "Funds vs. ETFs differ.", "Done!"]


def test_sentence_spans_split_on_question_marks_and_newlines():
    original = "Is it ready? Yes!\nOkay then"
    assert text.sentences(original) == ["Is it ready?", "Yes!", "Okay then"]


def test_sentence_spans_edge_cases():
    assert text.sentence_spans("") == []
    assert text.sentence_spans("   ") == []
    assert text.sentence_spans("no punctuation here") == [(0, len("no punctuation here"))]


@pytest.mark.parametrize("original, expected", [
    ("Please send it to us. We will review.", ["Please send it to us.", "We will review."]),
    ("I said no. Then we left.", ["I said no.", "Then we left."]),
    ("Call the U.S. team. Then we left.", ["Call the U.S. team.", "Then we left."]),          # the abbreviation still holds
])
def test_sentence_spans_split_after_the_pronoun_us_and_the_word_no(original, expected):
    assert text.sentences(original) == expected


@pytest.mark.parametrize("surface", [
    "Contoso Equity Fund, Class C",
    "Fondo Único — clase C!",
    "   many    spaces   here ",
    "U.S. Core Equity ETF",
    "",
])
def test_normalize_surface_matches_the_pipeline_normalisation(surface):
    assert text.normalize_surface(surface) == pipeline_normalize_surface(surface)


def test_normalize_surface_is_lowercase_accent_free_and_punctuation_free():
    assert text.normalize_surface("  Fondo CONTOSO, Clase-C (mañana)! ") == "fondo contoso clase c manana"


def test_norm_text_and_find_sequence_and_contains_phrase():
    assert text.norm_text("Fee-Comparison, por favor") == "fee comparison por favor"
    tokens = ["a", "b", "c", "b", "c"]
    assert text.find_sequence(tokens, ("b", "c")) == 1
    assert text.find_sequence(tokens, ("b", "c"), start=2) == 3
    assert text.find_sequence(tokens, ("z",)) == -1
    assert text.find_sequence(tokens, ()) == -1
    assert text.contains_phrase("send the fee comparison", "fee comparison")
    assert not text.contains_phrase("coffee comparison", "fee comparison")       # whole words only


def test_clip_collapses_whitespace_and_marks_truncation():
    assert text.clip("a   b\nc", 50) == "a b c"
    clipped = text.clip("word " * 40, 20)
    assert len(clipped) <= 20 and clipped.endswith("\u2026")

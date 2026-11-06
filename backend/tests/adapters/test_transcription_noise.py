"""ASR noise injector: determinism, identity at level 0, and the share-class / plural / article behaviours."""
from __future__ import annotations

import re

from voicecrm.adapters.transcription.noise import apply_asr_noise
from voicecrm.domain.conversation import Segment


def _seg(i: int, text: str, speaker: str = "seller", locale: str = "en-US") -> Segment:
    return Segment(id=i, speaker_id=speaker, start_ms=i * 1000, end_ms=i * 1000 + 900, text=text, locale=locale,
                   confidence=0.91)


CORPUS = [
    "Thanks for taking the call. We discussed the Contoso Equity Fund class C last week.",
    "Contoso Woodgrove has two funds that fit, the Income Fund class A and the Growth Fund class I.",
    "Can you send me the R6 class details for five of the bond funds?",
    "Hablamos del fondo de renta variable clase C y de los fondos de bonos.",
]


def test_level_zero_is_identity():
    segs = [_seg(i, t) for i, t in enumerate(CORPUS)]
    out = apply_asr_noise(segs, level=0)
    assert out == segs
    assert out is not segs and all(a is not b for a, b in zip(out, segs, strict=True))


def test_same_seed_same_output_and_input_not_mutated():
    segs = [_seg(i, t) for i, t in enumerate(CORPUS * 5)]
    snapshot = [s.model_copy() for s in segs]
    first = apply_asr_noise(segs, seed=7, level=1.0)
    second = apply_asr_noise(segs, seed=7, level=1.0)
    assert first == second
    assert segs == snapshot


def test_different_seeds_produce_different_noise():
    segs = [_seg(i, "We like the Contoso Equity Fund class C shares for two funds.") for i in range(30)]
    a = [s.text for s in apply_asr_noise(segs, seed=1, level=1.0)]
    b = [s.text for s in apply_asr_noise(segs, seed=2, level=1.0)]
    assert a != b


def test_only_text_changes():
    segs = [_seg(i, t, speaker="cust1" if i % 2 else "seller") for i, t in enumerate(CORPUS * 3)]
    out = apply_asr_noise(segs, seed=3, level=1.0)
    assert len(out) == len(segs)
    for before, after in zip(segs, out, strict=True):
        assert (after.id, after.speaker_id, after.start_ms, after.end_ms, after.locale, after.confidence) == (
            before.id, before.speaker_id, before.start_ms, before.end_ms, before.locale, before.confidence)
    assert any(a.text != b.text for a, b in zip(segs, out, strict=True))


def test_share_class_c_is_misheard_at_full_level():
    segs = [_seg(i, "We discussed Contoso Equity Fund class C yesterday.") for i in range(40)]
    out = [s.text for s in apply_asr_noise(segs, seed=7, level=1.0)]
    assert all(re.search(r"class (see|sea)|\bc class", t, re.IGNORECASE) for t in out)
    variants = {re.search(r"(class (?:see|sea)|c class)", t, re.IGNORECASE).group(1).lower() for t in out}
    assert len(variants) >= 2


def test_spanish_share_class_variants():
    segs = [_seg(i, "Hablamos del fondo clase C.", locale="es-US") for i in range(40)]
    out = [s.text for s in apply_asr_noise(segs, seed=7, level=1.0)]
    assert all(re.search(r"clase (ce|se)|\bce clase", t, re.IGNORECASE) for t in out)


def test_class_letter_is_never_dropped_as_an_article():
    segs = [_seg(i, "We hold the class A of the fund.") for i in range(60)]
    for text in (s.text for s in apply_asr_noise(segs, seed=11, level=1.0)):
        assert re.search(r"class eh|\ba class", text, re.IGNORECASE), text


def test_fund_plural_flips_both_ways():
    segs = [_seg(i, "Contoso Equity Fund") for i in range(60)]
    texts = {s.text for s in apply_asr_noise(segs, seed=5, level=1.0)}
    assert any("Equity Funds" in t for t in texts)
    assert "Contoso Equity Fund" in texts          # and sometimes left alone


def test_number_words_and_digits_swap():
    words = [_seg(i, "I looked at two funds") for i in range(80)]
    digits = [_seg(i, "I looked at 5 funds") for i in range(80)]
    assert any("2" in s.text.split() for s in apply_asr_noise(words, seed=1, level=1.0))
    assert any("five" in s.text.lower().split() for s in apply_asr_noise(digits, seed=1, level=1.0))


def test_share_class_tokens_like_r6_are_not_split_by_the_number_rule():
    segs = [_seg(i, "Please send the R6 details") for i in range(80)]
    assert all("Rsix" not in s.text for s in apply_asr_noise(segs, seed=4, level=1.0))


def test_text_without_error_sites_is_untouched():
    segs = [_seg(i, "Hello, thanks.") for i in range(10)]
    assert apply_asr_noise(segs, level=1.0) == segs


def test_level_is_clamped():
    segs = [_seg(i, t) for i, t in enumerate(CORPUS)]
    assert apply_asr_noise(segs, level=7.0) == apply_asr_noise(segs, level=1.0)
    assert apply_asr_noise(segs, level=-3.0) == segs


def test_lower_level_changes_less_text():
    segs = [_seg(i, "We discussed Contoso Equity Fund class C shares for the Income Fund.") for i in range(200)]
    low = sum(a.text != b.text for a, b in zip(segs, apply_asr_noise(segs, seed=2, level=0.15), strict=True))
    high = sum(a.text != b.text for a, b in zip(segs, apply_asr_noise(segs, seed=2, level=0.9), strict=True))
    assert low < high

"""Product resolver: mention detection, share-class linking, negation / correction handling, aliases and review routing."""
from __future__ import annotations

import pytest

from voicecrm.domain.catalogue import LearnedAlias
from voicecrm.domain.enums import MentionType
from voicecrm.engines.product_resolver import RulesProductResolver

from .helpers import seg, segments


def resolve(resolver, *turns, business_line="professional_buyer", language="en", top_k=3):
    segs = segments(*turns)
    mentions = resolver.resolve(resolver.detect_mentions(segs, language=language), business_line=business_line, top_k=top_k)
    return segs, mentions


def test_stated_class_with_a_negated_one_resolves_to_exactly_one_product(resolver):
    segs, mentions = resolve(resolver, ("c", "We are interested in the Contoso Equity Fund, class C, not class A."))
    assert [m.chosen_product_id for m in mentions] == ["SYN-000002"]
    m = mentions[0]
    assert m.share_class_stated == "C" and not m.needs_review and m.confidence >= 0.75
    assert m.mention_type == MentionType.interest
    assert m.candidates[0].product_id == "SYN-000002" and all(e.quote in segs[0].text for e in m.evidence)


def test_correction_across_turns_keeps_only_the_corrected_class(resolver):
    _, mentions = resolve(resolver,
                          ("c", "We currently hold the Contoso Equity Fund, Class A."),
                          ("s", "Hold on, it's not the Class A shares, it's the Contoso Equity Fund, Class C."))
    assert [m.chosen_product_id for m in mentions] == ["SYN-000002"]
    assert mentions[0].mention_type == MentionType.existing_holding            # the holding stance survives the correction


def test_class_never_stated_needs_review_with_candidates_from_the_same_fund(resolver, catalogue):
    _, (m,) = resolve(resolver, ("c", "We hold the Contoso Equity Fund."))
    assert m.chosen_product_id is None and m.needs_review and "share_class_not_stated" in m.reasons
    assert m.confidence < 0.75 and len(m.candidates) == 3
    products = catalogue.by_id()
    assert {products[c.product_id].family_id for c in m.candidates} == {"FEQ"}
    _, (retail,) = resolve(resolver, ("c", "We hold the Contoso Equity Fund."), business_line="retail", top_k=2)
    assert retail.candidates[0].share_class == "A" and m.candidates[0].share_class == "I"    # class prior per business line
    assert len(retail.candidates) == 2


def test_two_funds_in_one_sentence_keep_their_own_class_and_stance(resolver):
    text = "We hold the Contoso Equity Fund, class I and are considering the Contoso Equity Income Fund, Advisor class."
    segs, mentions = resolve(resolver, ("c", text))
    assert [m.chosen_product_id for m in mentions] == ["SYN-000005", "SYN-000008"]
    assert [m.mention_type for m in mentions] == [MentionType.existing_holding, MentionType.interest]
    assert [m.mention_id for m in mentions] == ["m1", "m2"]
    assert all(e.quote in segs[0].text for m in mentions for e in m.evidence)


def test_brandless_reference_merges_with_the_full_name_used_earlier(resolver):
    _, mentions = resolve(resolver, ("c", "We like the Contoso Global Equity UCITS Fund."),
                          ("s", "Right, the global equity fund has done well."))
    assert len(mentions) == 1
    assert mentions[0].surface == "Contoso Global Equity UCITS Fund"
    assert {e.segment_ids[0] for e in mentions[0].evidence} == {0, 1}


def test_category_talk_firm_name_and_sensitive_segments_are_not_products(resolver):
    generic = resolver.detect_mentions(segments(("c", "We are looking at a growth fund for the sleeve.")), language="en")
    assert generic and not any(raw.branded for raw in generic)
    assert resolve(resolver, ("c", "We are looking at a growth fund for the sleeve."))[1] == []
    assert resolve(resolver, ("c", "We work with Contoso Woodgrove on a few mandates."))[1] == []
    leaked = [seg(0, "We want the Contoso Equity Fund, class C.", "c", label="sensitive")]
    assert resolver.detect_mentions(leaked, language="en") == []


def test_etf_resolves_without_a_share_class(resolver):
    _, (m,) = resolve(resolver, ("c", "We hold the Contoso U.S. Core Equity ETF."))
    assert m.chosen_product_id == "SYN-000054" and not m.needs_review
    assert m.share_class_stated is None and m.mention_type == MentionType.existing_holding


def test_etf_word_in_the_next_turn_is_not_taken_as_a_share_class(resolver):
    _, (m,) = resolve(resolver, ("c", "We hold the Contoso Equity Fund."), ("s", "Are you also considering the ETF version?"))
    assert m.share_class_stated is None and m.reasons == ["share_class_not_stated"]


def test_spanish_class_phrase_with_negation(resolver):
    _, (m,) = resolve(resolver, ("c", "Queremos el Contoso Equity Fund, clase C, no la clase A."), language="es")
    assert m.chosen_product_id == "SYN-000002" and m.mention_type == MentionType.interest and not m.needs_review


def test_class_answered_in_the_next_turn_is_linked(resolver):
    _, (m,) = resolve(resolver, ("c", "We are interested in the Contoso Equity Fund."), ("s", "Which share class?"),
                      ("c", "Class C."))
    assert m.chosen_product_id == "SYN-000002" and m.evidence[0].segment_ids == [0, 2]


def test_ruled_out_and_unavailable_classes_never_become_the_choice(resolver):
    _, (ruled_out,) = resolve(resolver, ("c", "We want the Contoso Equity Fund, not class A."))
    assert ruled_out.chosen_product_id is None and "A" not in {c.share_class for c in ruled_out.candidates}
    _, (missing,) = resolve(resolver, ("c", "We want the Contoso Equity Fund, class W."))       # this fund has no class W
    assert missing.chosen_product_id is None and missing.needs_review and "share_class_unavailable" in missing.reasons


@pytest.mark.parametrize("text, expected", [
    ("We are concerned about the Contoso Equity Fund, class C.", MentionType.objection),
    ("We already own the Contoso Equity Fund, class C.", MentionType.existing_holding),
    ("We are considering the Contoso Equity Fund, class C.", MentionType.interest),
])
def test_stance_follows_the_wording_around_the_fund_name(resolver, text, expected):
    _, (m,) = resolve(resolver, ("c", text))
    assert m.mention_type == expected and m.chosen_product_id == "SYN-000002"


def test_learned_alias_resolves_a_legacy_name_the_catalogue_does_not_know(catalogue, cindex):
    resolver = RulesProductResolver(catalogue, index=cindex)
    turns = [("c", "We hold the Contoso Blue Chip Fund, class C.")]
    _, (before,) = resolve(resolver, *turns)
    assert before.chosen_product_id is None and before.needs_review and "no_candidate" in before.reasons
    resolver.reload_aliases([LearnedAlias(surface="contoso blue chip fund class c", product_id="SYN-000002"),
                             LearnedAlias(surface="contoso zephyr fund class c", product_id="NOT-IN-CATALOGUE")])
    _, (after,) = resolve(resolver, *turns)
    assert after.chosen_product_id == "SYN-000002" and not after.needs_review and "learned_alias" in after.reasons
    _, (ignored,) = resolve(resolver, ("c", "We hold the Contoso Zephyr Fund, class C."))
    assert ignored.chosen_product_id is None and "no_candidate" in ignored.reasons     # alias to an unknown product is ignored
    resolver.reload_aliases([])
    assert resolve(resolver, *turns)[1][0].chosen_product_id is None


def test_a_learned_alias_never_overrides_the_share_class_the_speaker_said(catalogue, cindex):
    resolver = RulesProductResolver(catalogue, index=cindex)
    resolver.reload_aliases([LearnedAlias(surface="contoso blue chip fund class c", product_id="SYN-000002")])
    for other_class in ("class A", "class R6"):
        _, (m,) = resolve(resolver, ("c", f"We hold the Contoso Blue Chip Fund, {other_class}."))
        assert m.chosen_product_id is None and m.needs_review and "no_candidate" in m.reasons
        assert m.share_class_stated != "C"
    _, (same_class,) = resolve(resolver, ("c", "We hold the Contoso Blue Chip Funds, class C."))     # ASR-style noise, same class
    assert same_class.chosen_product_id == "SYN-000002" and "learned_alias" in same_class.reasons


def test_phrase_list_contains_class_phrases_and_fund_names(resolver):
    phrases = resolver.phrase_list()
    assert {"Class C", "Contoso Equity Fund"} <= set(phrases)
    assert len(resolver.phrase_list(50)) <= 50

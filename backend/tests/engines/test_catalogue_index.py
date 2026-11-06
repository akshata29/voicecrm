"""Catalogue index: fund-name spans, family matching, share-class phrases and the offline search port."""
from __future__ import annotations

import pytest

from voicecrm.engines.catalogue_index import LocalCatalogueSearch
from voicecrm.engines.text import norm_words, tokenize


def spans(cindex, text):
    toks = tokenize(text)
    return [(s, " ".join(t.text for t in toks[s.start:s.end])) for s in cindex.find_fund_spans(toks)]


def families(cindex, text, limit=4):
    return cindex.match_families(norm_words(text), limit=limit)


def classes(cindex, text):
    return [h.cls for h in cindex.find_classes(tokenize(text))]


def test_exact_fund_name_is_found_and_matched_exactly(cindex):
    found = spans(cindex, "We are interested in the Contoso Equity Fund, class C, not class A.")
    assert [surface for _, surface in found] == ["Contoso Equity Fund"]
    assert found[0][0].has_brand and found[0][0].has_fund_word
    top = families(cindex, "Contoso Equity Fund")[0]
    assert (top.family.family_id, top.exact, top.brand_mismatch) == ("FEQ", True, False)
    assert set(top.family.products) == {"A", "C", "Advisor", "R6", "I"}


def test_brandless_specific_name_beats_the_generic_one(cindex):
    income = families(cindex, "the equity income fund")
    assert income[0].family.family_id == "FEI" and income[0].exact
    generic = families(cindex, "the equity fund")
    assert generic[0].family.family_id == "FEQ" and generic[0].exact
    # "equity income" must not be confused with plain "equity" (the resolver treats a <0.06 margin as ambiguous)
    assert income[0].score - income[1].score > 0.06
    assert generic[0].score - generic[1].score > 0.06


def feq_match(cindex, text):
    return next(m for m in families(cindex, text, limit=10) if m.family.family_id == "FEQ")


def test_brand_mismatch_is_penalised(cindex):
    same, none, other = (feq_match(cindex, t) for t in ("Contoso Equity Fund", "Equity Fund", "Woodgrove Equity Fund"))
    assert not same.brand_mismatch and other.brand_mismatch
    assert other.score < none.score <= same.score
    assert not other.exact


def test_asr_slips_are_mapped_onto_the_catalogue_vocabulary(cindex):
    ((span, _),) = spans(cindex, "Contosa Equity Funds")
    assert span.tokens == ["contoso", "equity", "funds"]
    assert families(cindex, "Contosa Equity Funds")[0].family.family_id == "FEQ"
    ((split, _),) = spans(cindex, "Wood grove Global Equity fund")              # recogniser split the brand in two
    assert "woodgrove" in split.tokens
    assert cindex.canon("contosa") == "contoso" and cindex.canon("gibberish") is None


def test_aliases_match_in_spanish_and_english(cindex):
    top = families(cindex, "Fondo Contoso de Acciones con Ingresos")[0]
    assert top.family.family_id == "FEI" and top.via_alias


def test_firm_name_is_not_a_fund_but_an_unknown_branded_name_is_surfaced(cindex):
    assert spans(cindex, "We work with Contoso Woodgrove every day") == []
    ((span, _),) = spans(cindex, "Contoso Blue Chip Fund")
    assert span.has_brand and cindex.match_families(span.tokens) == []             # legacy name: a span, no candidates


@pytest.mark.parametrize("phrase, expected", [
    ("class C", "C"), ("clase C", "C"), ("Clase C", "C"), ("class see", "C"), ("Series C", "C"),
    ("R6", "R6"), ("class are six", "R6"), ("advisor class", "Advisor"), ("institutional class", "I"),
    ("acciones clase A", "A"), ("A shares", "A"), ("class I", "I"),
])
def test_class_phrases_in_english_spanish_and_asr_spellings(cindex, phrase, expected):
    assert classes(cindex, phrase) == [expected]


@pytest.mark.parametrize("phrase", ["a shares", "i shares", "class a", "class i", "a class of investors", "I think a fund works"])
def test_bare_lowercase_a_and_i_are_not_share_classes(cindex, phrase):
    assert classes(cindex, phrase) == []


def test_etf_is_a_vehicle_not_a_share_class_of_mutual_fund_families(catalogue, cindex):
    assert {p.vehicle for p in catalogue.products if p.share_class == "ETF"} == {"ETF"}
    assert set(cindex.families["FCE"].products) == {"ETF"}                           # a single-product family, no class to choose
    for line in ("retail", "professional_buyer", "international"):
        assert "ETF" not in cindex.class_order(cindex.families["FEQ"], line)


def test_class_order_follows_the_business_line_prior(cindex):
    fam = cindex.families["FEQ"]
    assert cindex.class_order(fam, "retail")[0] == "A"
    assert cindex.class_order(fam, "professional_buyer")[0] == "I"
    assert cindex.class_order(fam, "unknown_line") == cindex.class_order(fam, "professional_buyer")
    assert sorted(cindex.class_order(fam, "retail")) == sorted(fam.products)


async def test_local_search_ranks_the_named_class_first(cindex):
    search = LocalCatalogueSearch(cindex=cindex)
    hits = await search.search("Contoso Equity Fund class C", top=3)
    assert hits[0].id == "SYN-000002" and hits[0].fields["share_class"] == "C"
    assert hits[1].fields["share_class"] == "C"                                      # other funds' class C follow, not class A
    assert len(await search.search("Contoso Equity Fund", top=7)) == 7
    assert await search.search("") == []


async def test_local_search_equity_income_advisor_top_hit_is_the_advisor_class(cindex):
    search = LocalCatalogueSearch(cindex=cindex)
    hits = await search.search("equity income advisor")
    assert hits[0].id == "SYN-000008"                                                # Contoso Equity Income Fund Advisor Class
    assert hits[0].fields["fund_name"] == "Contoso Equity Income Fund"
    assert {h.fields["share_class"] for h in hits[:3]} == {"Advisor"}                # the class word applies to every family
    assert (await search.search("equity income advisor", business_line="retail"))[0].id == "SYN-000008"


def test_phrase_list_has_class_phrases_and_fund_names_without_duplicates(cindex):
    phrases = cindex.phrase_list(500)
    assert {"Class C", "Contoso Equity Fund", "Clase C"} <= set(phrases)
    assert len(phrases) <= 500 and len({p.lower() for p in phrases}) == len(phrases)
    assert len(cindex.phrase_list(100)) <= 100


@pytest.mark.parametrize("limit", [0, 1, 5, 14, 15, 100])
def test_phrase_list_honours_the_limit_exactly(cindex, limit):
    assert len(cindex.phrase_list(limit)) == limit

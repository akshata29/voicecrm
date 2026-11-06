"""Topic engine: lexical taxonomy classification with evidence, thresholds, business-line scoping and a cap of five."""
from __future__ import annotations

import pytest

from voicecrm.engines.topics import RulesTopicEngine

from .helpers import seg, segments


def classify(engine, *turns, business_line="professional_buyer", language="en"):
    segs = segments(*turns)
    return segs, engine.classify(segs, language=language, business_line=business_line)


def ids(tags):
    return [t.topic_id for t in tags]


@pytest.mark.parametrize("text, topic", [
    ("What are the fees on this fund? The expense ratio looks high.", "fees_expenses"),
    ("Taxes matter to us because the account is taxable and we worry about capital gains.", "tax_efficiency"),
    ("We are building a retirement glide path for the target date series.", "retirement_target_date"),
])
def test_english_topics_are_tagged_with_taxonomy_labels(topic_engine, taxonomy, text, topic):
    _, tags = classify(topic_engine, ("c", text))
    assert topic in ids(tags)
    tag = next(t for t in tags if t.topic_id == topic)
    assert tag.label == taxonomy.by_id()[topic].label and 0.5 < tag.confidence <= 0.95


@pytest.mark.parametrize("text, topic", [
    ("Nos preocupan las comisiones y los gastos del fondo.", "fees_expenses"),
    ("Buscamos una solución de jubilación con fecha objetivo para nuestros clientes.", "retirement_target_date"),
    ("¿Cuál es el tratamiento de los impuestos y las ganancias de capital?", "tax_efficiency"),
])
def test_spanish_synonyms_are_matched_accent_insensitively(topic_engine, text, topic):
    _, tags = classify(topic_engine, ("c", text), language="es")
    assert topic in ids(tags)


def test_a_single_weak_keyword_is_below_the_threshold_but_hits_in_two_turns_add_up(topic_engine):
    assert classify(topic_engine, ("c", "The fee is fine."))[1] == []
    _, tags = classify(topic_engine, ("c", "We discussed fees yesterday."), ("c", "The fee is fine."))
    assert ids(tags) == ["fees_expenses"] and [e.segment_ids for e in tags[0].evidence] == [[0], [1]]


def test_customer_turns_weigh_slightly_more_than_seller_turns(topic_engine):
    (_, customer), (_, seller) = classify(topic_engine, ("c", "fee schedule")), classify(topic_engine, ("s", "fee schedule"))
    assert ids(customer) == ids(seller) == ["fees_expenses"]
    assert customer[0].confidence > seller[0].confidence


def test_sensitive_and_small_talk_segments_never_contribute(topic_engine):
    segs = [seg(0, "", "c", label="sensitive"),
            seg(1, "fees fees expense ratio taxes retirement target date", "c", label="small_talk"),
            seg(2, "My wife has cancer; the fees and the expense ratio worry me.", "c", label="sensitive")]
    assert topic_engine.classify(segs, language="en", business_line="professional_buyer") == []
    segs.append(seg(3, "What are the fees and the expense ratio?", "c"))
    tags = topic_engine.classify(segs, language="en", business_line="professional_buyer")
    assert ids(tags) == ["fees_expenses"] and tags[0].evidence[0].segment_ids == [3]


def test_evidence_quotes_are_verbatim_segment_text_and_capped_at_two(topic_engine):
    turns = [("c", "Hello again. What are the fees on this fund? Thanks."), ("s", "Our fee schedule is competitive."),
             ("c", "The expense ratio is a concern."), ("c", "We also compared the taxes. Fees matter most.")]
    segs, tags = classify(topic_engine, *turns)
    texts = {s.id: s.text for s in segs}
    assert tags and all(e.quote and e.quote in texts[e.segment_ids[0]] for t in tags for e in t.evidence)
    fees = next(t for t in tags if t.topic_id == "fees_expenses")
    assert len(fees.evidence) == 2 and fees.evidence[0].segment_ids[0] < fees.evidence[1].segment_ids[0]
    _, (single,) = classify(topic_engine, ("c", "Hello again. What are the fees and the expense ratio on this fund? Thanks."))
    assert single.evidence[0].quote == "What are the fees and the expense ratio on this fund?"   # the sentence, not the turn


def test_at_most_five_topics_best_first_with_a_stable_tie_break(topic_engine):
    turns = [("c", "What are the fees and expense ratio? Also the performance and returns and track record."),
             ("c", ("Risk and volatility and drawdown worry us; ESG and sustainability matter; rate cuts and interest rates; "
                    "allocation and rebalancing; taxes and capital gains; retirement and target date.")),
             ("c", "Competitor compared to Vanguard and a redemption and inflows question."),
             ("c", "Please send the fact sheet and the presentation deck; market outlook, inflation and recession.")]
    _, tags = classify(topic_engine, *turns)
    assert len(tags) == 5
    assert [t.confidence for t in tags] == sorted((t.confidence for t in tags), reverse=True)
    assert ids(tags) == ids(classify(topic_engine, *turns)[1])                       # deterministic


def test_topic_business_lines_restrict_what_is_offered(topic_engine):
    text = "We need due diligence, the DDQ and RFP questionnaire for approval."
    assert ids(classify(topic_engine, ("c", text), business_line="professional_buyer")[1]) == ["due_diligence_approval"]
    assert ids(classify(topic_engine, ("c", text), business_line="international")[1]) == ["due_diligence_approval"]
    assert classify(topic_engine, ("c", text), business_line="retail")[1] == []


def test_threshold_and_cap_are_configurable(taxonomy):
    eager = RulesTopicEngine(taxonomy, threshold=0.4, max_topics=1)
    tags = eager.classify(segments(("c", "The fee is fine, but taxes and retirement matter.")), language="en",
                          business_line="retail")
    assert len(tags) == 1                                                            # three weak hits, capped at one
    assert RulesTopicEngine(taxonomy, threshold=0.4).classify(segments(("c", "The fee is fine.")), language="en",
                                                              business_line="retail")[0].topic_id == "fees_expenses"

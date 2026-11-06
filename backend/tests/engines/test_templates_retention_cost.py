"""Template selection + markdown rendering, per-market retention decisions and the (illustrative) pricing cost model."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voicecrm.domain.enums import Direction, Engine, Stage
from voicecrm.domain.extraction import Evidence, ExtractionResult, NoteSection, TopicTag
from voicecrm.domain.metering import LlmPrice, PricingTable, UsageRecord
from voicecrm.domain.policy import MarketPolicy, ProcessingPolicy
from voicecrm.engines.cost import PricingCostEngine
from voicecrm.engines.retention import PolicyRetentionEngine
from voicecrm.engines.templates import MarkdownNoteRenderer, select_template

from .helpers import make_context, make_meta

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------- template selection
def test_requested_template_wins_over_the_business_line_default(templates, policies):
    chosen = select_template(templates, make_meta("retail"), policies.processing, "pb_institutional")
    assert chosen.template_id == "pb_institutional"


def test_default_template_comes_from_the_processing_policy(templates, policies):
    for line, expected in [("professional_buyer", "pb_institutional"), ("international", "pb_institutional"),
                           ("retail", "retail_advisor")]:
        assert select_template(templates, make_meta(line), policies.processing).template_id == expected
    swapped = ProcessingPolicy(default_template_by_business_line={"professional_buyer": "retail_advisor"})
    assert select_template(templates, make_meta("professional_buyer"), swapped).template_id == "retail_advisor"


def test_unknown_requested_template_is_a_key_error(templates, policies):
    with pytest.raises(KeyError, match="nope"):
        select_template(templates, make_meta(), policies.processing, "nope")


def test_without_a_policy_default_the_best_fitting_template_is_used(templates):
    bare = ProcessingPolicy()
    assert select_template(templates, make_meta("retail"), bare).template_id == "retail_advisor"
    assert select_template(templates, make_meta("professional_buyer"), bare).template_id == "pb_institutional"
    assert select_template(templates, make_meta("wealth"), bare).template_id == next(iter(templates))     # nothing fits
    with pytest.raises(KeyError):
        select_template({}, make_meta(), bare)


# ---------------------------------------------------------------------------------------------- markdown rendering
def topic_tag(topic_id: str, label: str) -> TopicTag:
    return TopicTag(topic_id=topic_id, label=label, confidence=0.8, evidence=[Evidence(segment_ids=[0], quote="q")])


def sample_result(template) -> ExtractionResult:
    contents = {"overview": ("Sam Seller spoke with Priya Nair.", []),
                "client_context": (None, ["Pension mandate", "Decision in December"]),
                "products_strategies": (None, []), "concerns": (None, ["Fees look high"]), "decisions_next_steps": (None, [])}
    sections = [NoteSection(key=s.key, title=s.title, text=contents[s.key][0], items=contents[s.key][1])
                for s in template.sections]
    return ExtractionResult(template_id=template.template_id, sections=sections, overall_confidence=0.8, engine=Engine.rules,
                            topics=[topic_tag("fees_expenses", "Fees and expenses"), topic_tag("tax_efficiency", "Tax considerations")])


def test_markdown_has_headings_in_template_order_bullets_empty_markers_and_topics(templates, catalogue):
    template, renderer = templates["pb_institutional"], MarkdownNoteRenderer()
    result = sample_result(template)
    result.sections.reverse()                                                       # the renderer, not the caller, owns the order
    md = renderer.render(result, template, make_meta(), catalogue=catalogue)
    lines = md.splitlines()
    assert lines[0] == f"# {template.name}"
    assert all(part in lines[1] for part in ("Teams meeting", "2026-11-10", "Sam Seller", "Priya Nair"))
    assert [ln for ln in lines if ln.startswith("## ")] == [f"## {s.title}" for s in template.sections]
    assert "Sam Seller spoke with Priya Nair." in md
    assert "- Pension mandate" in lines and "- Decision in December" in lines and "- Fees look high" in lines
    assert md.count("_None noted._") == 2                                           # products and next steps are empty
    assert lines[-1] == "**Topics:** Fees and expenses | Tax considerations" and md.endswith("\n") and not md.endswith("\n\n")


def test_markdown_context_line_reflects_direction_and_extra_sections_come_last(templates, catalogue):
    template = templates["pb_institutional"]
    result = sample_result(template)
    result.sections.insert(0, NoteSection(key="extra", title="Extra", items=["bonus"]))
    result.topics = []
    meta = make_meta().model_copy(update={"direction": Direction.inbound})
    md = MarkdownNoteRenderer().render(result, template, meta, catalogue=catalogue)
    assert md.splitlines()[1].startswith("*Inbound teams meeting")
    headings = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert headings[-1] == "## Extra" and "**Topics:**" not in md


async def test_rendered_note_carries_no_transcript_text(templates, catalogue, rules_extractor):
    template = templates["pb_institutional"]
    ctx = make_context([("c", "We are interested in the Contoso Equity Fund, class C, for our pension mandate."),
                        ("c", "My card number is 4111 1111 1111 1111."),
                        ("c", "My wife was diagnosed with cancer, so timing is tight."),
                        ("s", "I'll send you the fee comparison by Friday.")], template)
    result = (await rules_extractor.extract(ctx)).result
    md = MarkdownNoteRenderer().render(result, template, ctx.conversation.meta, catalogue=catalogue)
    quotes = [e.quote for m in result.products for e in m.evidence] + [e.quote for t in result.tasks for e in t.evidence]
    assert quotes and not any(q in md for q in quotes)
    assert "4111" not in md and "cancer" not in md.lower() and "wife" not in md.lower()


# ---------------------------------------------------------------------------------------------- retention
def test_us_market_stores_no_transcript(policies):
    decision = PolicyRetentionEngine().decide(policies.markets["US"], now=NOW)
    assert decision.market == "US" and decision.transcript_stored is False
    assert decision.transcript_expires_at is None and decision.audio_expires_at is None
    assert decision.crm_contains_transcript is False and "OFF" in decision.explanation


def test_eu_market_keeps_a_redacted_transcript_for_five_years(policies):
    decision = PolicyRetentionEngine().decide(policies.markets["EU"], now=NOW)
    assert decision.transcript_stored and decision.transcript_redacted
    assert decision.transcript_expires_at == NOW + timedelta(days=1825)
    assert decision.audio_expires_at is None and decision.crm_contains_transcript is False
    assert "1825" in decision.explanation and "ON" in decision.explanation


def test_the_crm_never_holds_the_transcript_in_any_market(policies):
    for market in policies.markets.values():
        assert PolicyRetentionEngine().decide(market, now=NOW).crm_contains_transcript is False


def test_audio_expiry_open_ended_retention_and_unredacted_storage():
    policy = MarketPolicy(market="XX", display_name="Testland", transcript_retention="on", store_redacted_only=False,
                          audio_retention_days=30)
    decision = PolicyRetentionEngine().decide(policy, now=NOW)
    assert decision.audio_expires_at == NOW + timedelta(days=30)
    assert decision.transcript_stored and decision.transcript_expires_at is None and decision.transcript_redacted is False


# ---------------------------------------------------------------------------------------------- cost
PRICING = PricingTable(speech_per_audio_hour=1.2, llm={"default": LlmPrice(input_per_1m_tokens=2, output_per_1m_tokens=8),
                                                       "gpt-x": LlmPrice(input_per_1m_tokens=1, output_per_1m_tokens=3)},
                       language_pii_per_1k_records=1.0, search_per_1k_queries=0.5)


def usage(service, provider="azure", **fields) -> UsageRecord:
    return UsageRecord(conversation_id="c1", stage=Stage.extract, service=service, provider=provider, **fields)


def lines_by_service(breakdown):
    return {line.service: line for line in breakdown.lines}


def test_speech_seconds_are_billed_per_audio_hour():
    cost = PricingCostEngine().estimate([usage("speech", audio_seconds=1800), usage("speech", audio_seconds=1800)], PRICING)
    speech = lines_by_service(cost)["speech"]
    assert speech.quantity == pytest.approx(1.0) and speech.est_cost == pytest.approx(1.2) and cost.total == pytest.approx(1.2)


def test_llm_tokens_are_priced_per_model_with_a_default_fallback():
    records = [usage("llm", model="gpt-x", tokens_in=1_000_000, tokens_out=500_000),
               usage("llm", model="mystery", tokens_in=500_000, tokens_out=250_000),
               usage("llm", model=None, tokens_in=1_000_000, tokens_out=0)]
    cost = PricingCostEngine().estimate(records, PRICING)
    lines = lines_by_service(cost)
    assert lines["llm:gpt-x"].est_cost == pytest.approx(1 * 1.0 + 0.5 * 3.0)
    assert lines["llm:mystery"].est_cost == pytest.approx(0.5 * 2.0 + 0.25 * 8.0)          # unknown model -> "default" prices
    assert lines["llm:default"].est_cost == pytest.approx(2.0)
    assert cost.total == pytest.approx(2.5 + 3.0 + 2.0)
    no_default = PricingTable(llm={})
    assert PricingCostEngine().estimate(records, no_default).total == 0.0


def test_language_pii_and_search_are_billed_per_thousand():
    cost = PricingCostEngine().estimate([usage("language_pii", units=2500), usage("search", units=4000)], PRICING)
    lines = lines_by_service(cost)
    assert lines["language_pii"].est_cost == pytest.approx(2.5) and lines["search"].est_cost == pytest.approx(2.0)
    assert cost.total == pytest.approx(4.5)


def test_projected_llm_usage_is_shown_but_not_billed():
    records = [usage("speech", audio_seconds=3600),
               usage("llm", provider="projected", model="gpt-x", tokens_in=1_000_000, tokens_out=1_000_000)]
    cost = PricingCostEngine().estimate(records, PRICING)
    projected = next(line for line in cost.lines if line.service.startswith("llm_projected"))
    assert projected.est_cost == pytest.approx(10.0)                                       # priced at the "default" model
    assert cost.total == pytest.approx(1.2) and not any(line.service.startswith("llm:") for line in cost.lines)


def test_empty_usage_costs_nothing_and_carries_the_currency():
    cost = PricingCostEngine().estimate([], PricingTable(currency="EUR", illustrative=False))
    assert cost.lines == [] and cost.total == 0.0 and cost.currency == "EUR" and cost.illustrative is False

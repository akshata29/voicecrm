"""Summariser + extractors: the rules extractor end to end, and the LLM / hybrid extractor reconciled against the rules."""
from __future__ import annotations

import json
from datetime import date

import pytest

from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.domain.enums import Engine, Stage
from voicecrm.engines.extractor import LlmExtractor, build_extractors
from voicecrm.engines.summarizer import RulesSummaryEngine, third_person
from voicecrm.engines.tasks import RulesTaskEngine

from .helpers import llm_reply as reply
from .helpers import make_context, task_answer

TURNS = [
    ("s", "Good morning Priya, thanks for joining."),                                                       # 0 small talk
    ("c", ("We are evaluating the Contoso Equity Income Fund, Advisor class, for our pension mandate, and the investment "
           "committee needs a decision by December.")),                                                     # 1
    ("s", "Great. Which share class do you currently hold in the portfolio?"),                              # 2
    ("c", ("We currently hold the Contoso Equity Fund, class I, and the fees are a concern because the expense ratio "
           "looks higher than our benchmark.")),                                                            # 3
    ("s", "I'll send you the fee comparison by Friday."),                                                   # 4
    ("c", "Also my card is 4111 1111 1111 1111 for the subscription."),                                     # 5 PII
    ("c", "My wife was diagnosed with cancer so timing is tight."),                                         # 6 sensitive
    ("s", "Understood. I'll also schedule a call with the portfolio manager next Tuesday."),                # 7
    ("c", "I'm a huge Seahawks fan, by the way."),                                                          # 8 relationship
    ("c", "Our committee wants to understand the tax treatment and the retirement target date glide path."),  # 9
    ("s", "Let me get the updated fact sheet over to you tomorrow."),                                       # 10
]
SPANISH = [("s", "Buenos días Priya, gracias por conectarse."),
           ("c", "Queremos el Contoso Equity Fund, clase C, no la clase A."),
           ("c", "Nos preocupan las comisiones y los gastos del fondo."),
           ("s", "Le envío la comparación de comisiones el viernes.")]
NO_CLASS = [("c", "We hold the Contoso Equity Fund and the fees are a concern because the expense ratio looks high."),
            ("s", "I'll send you the fee comparison by Friday.")]


@pytest.fixture
def pb_ctx(templates):
    return make_context(TURNS, templates["pb_institutional"])


@pytest.fixture
def retail_ctx(templates):
    return make_context(TURNS, templates["retail_advisor"], business_line="retail")


@pytest.mark.parametrize("template_id, line", [("pb_institutional", "professional_buyer"), ("retail_advisor", "retail")])
async def test_every_template_section_is_present_in_order_within_its_limits(rules_extractor, templates, template_id, line):
    template = templates[template_id]
    result = (await rules_extractor.extract(make_context(TURNS, template, business_line=line))).result
    assert [(s.key, s.title) for s in result.sections] == [(s.key, s.title) for s in template.sections]
    for section, spec in zip(result.sections, template.sections, strict=True):
        if spec.kind == "list":
            assert section.text is None and len(section.items) <= spec.max_items
        else:
            assert section.text and section.items == []


async def test_list_sections_are_cut_to_max_items(rules_extractor, templates):
    template = templates["pb_institutional"].model_copy(deep=True)
    for spec in template.sections:
        if spec.key in {"products_strategies", "decisions_next_steps"}:
            spec.max_items = 1
    result = (await rules_extractor.extract(make_context(TURNS, template))).result
    items = {s.key: s.items for s in result.sections}
    assert len(items["products_strategies"]) == len(items["decisions_next_steps"]) == 1
    assert len(result.products) == 2 and len(result.tasks) == 3                    # structured results are not truncated


async def test_products_topics_and_tasks_are_populated_and_mirrored_in_the_note(rules_extractor, pb_ctx):
    result = (await rules_extractor.extract(pb_ctx)).result
    assert [m.chosen_product_id for m in result.products] == ["SYN-000008", "SYN-000005"]
    assert {"fees_expenses", "tax_efficiency", "retirement_target_date"} <= {t.topic_id for t in result.topics}
    assert [t.due_date for t in result.tasks] == [date(2026, 11, 13), date(2026, 11, 17), date(2026, 11, 11)]
    next_steps = next(s for s in result.sections if s.key == "decisions_next_steps")
    assert all(task.title in item and task.due_date.isoformat() in item for task, item in zip(result.tasks, next_steps.items, strict=True))
    products = next(s for s in result.sections if s.key == "products_strategies")
    assert "Contoso Equity Income Fund Advisor Class" in products.items[0]


async def test_engine_confidence_flags_and_redacted_only_content(rules_extractor, pb_ctx):
    out = await rules_extractor.extract(pb_ctx)
    result = out.result
    assert result.engine == Engine.rules and result.model is None and out.usage == [] and rules_extractor.name == "rules"
    assert 0.0 <= result.overall_confidence <= 1.0
    assert "pricing_discussed" in result.flags and "free_text_needs_llm_engine" not in result.flags
    dump = result.model_dump_json().lower()
    assert not [needle for needle in ("4111", "cancer", "wife", "thanks for joining") if needle in dump]


async def test_relationship_facts_only_for_templates_that_carry_them(rules_extractor, pb_ctx, retail_ctx):
    assert (await rules_extractor.extract(pb_ctx)).result.relationship_facts == []
    retail = (await rules_extractor.extract(retail_ctx)).result
    assert [f.text for f in retail.relationship_facts] == ["Supports the Seattle Seahawks"]
    assert next(s for s in retail.sections if s.key == "relationship_notes").items == ["Supports the Seattle Seahawks"]


async def test_spanish_call_gets_a_structured_note_and_a_free_text_flag(rules_extractor, templates):
    result = (await rules_extractor.extract(make_context(SPANISH, templates["pb_institutional"], language="es"))).result
    assert {"free_text_needs_llm_engine", "source_language:es"} <= set(result.flags)
    assert [m.chosen_product_id for m in result.products] == ["SYN-000002"] and result.tasks[0].due_date == date(2026, 11, 13)
    sections = {s.key: s for s in result.sections}
    assert "held in Spanish" in sections["overview"].text and sections["client_context"].items == []


async def test_competitor_mentions_are_flagged_and_unresolved_products_say_so(rules_extractor, templates):
    ctx = make_context([("c", "We also use Vanguard for the core sleeve."), *NO_CLASS], templates["pb_institutional"])
    result = (await rules_extractor.extract(ctx)).result
    assert "competitor_named" in result.flags and result.products[0].chosen_product_id is None
    assert "need a share class" in next(s for s in result.sections if s.key == "overview").text
    assert "share class to be confirmed" in next(s for s in result.sections if s.key == "products_strategies").items[0]


async def test_word_budget_trims_context_lists_and_no_tasks_is_stated(rules_extractor, templates):
    tight = templates["pb_institutional"].model_copy(update={"max_words": 60})
    full = {s.key: s for s in (await rules_extractor.extract(make_context(TURNS, templates["pb_institutional"]))).result.sections}
    trimmed = {s.key: s for s in (await rules_extractor.extract(make_context(TURNS, tight))).result.sections}
    assert len(full["client_context"].items) == 2 and len(trimmed["client_context"].items) < 2
    quiet = (await rules_extractor.extract(make_context([("c", "We like the Contoso Equity Fund, class C.")],
                                                        templates["pb_institutional"]))).result
    assert quiet.tasks == [] and "No follow-up actions" in next(s for s in quiet.sections if s.key == "decisions_next_steps").items[0]


def test_third_person_rewrites_first_person_pronouns():
    rewritten = third_person("We're evaluating our options and I'll decide.")
    assert rewritten.startswith("They are evaluating their options") and "they will decide" in rewritten
    assert third_person("Yeah, so we need it for my committee.") == "They need it for their committee."


def llm_extractor(rules_extractor, taxonomy, *, mode="hybrid", **overrides):
    llm = FakeLlm(lambda call: reply(call, **overrides))
    return llm, LlmExtractor(llm, rules_extractor, taxonomy, mode=mode)


@pytest.mark.parametrize("mode, engine", [("hybrid", Engine.hybrid), ("llm", Engine.llm)])
async def test_a_well_formed_model_answer_is_used_and_its_usage_recorded(rules_extractor, taxonomy, pb_ctx, templates,
                                                                          mode, engine):
    llm, extractor = llm_extractor(rules_extractor, taxonomy, mode=mode)
    out = await extractor.extract(pb_ctx)
    result = out.result
    assert result.engine == engine and result.model == "fake-llm" and extractor.name == mode
    sections = {s.key: s for s in result.sections}
    assert sections["overview"].text == "Model note: overview"
    template = templates["pb_institutional"]
    assert [len(sections[s.key].items) for s in template.sections if s.kind == "list"] == [
        s.max_items for s in template.sections if s.kind == "list"]                  # the model's 9 items are cut to the limits
    assert [(t.topic_id, t.label) for t in result.topics] == (
        [("fees_expenses", "Fees and expenses")] if mode == "llm"
        else [(t.topic_id, t.label) for t in (await rules_extractor.extract(pb_ctx)).result.topics])   # hybrid keeps the rules' topics
    (task,) = result.tasks
    assert (task.title, task.due_date, task.owner_role.value) == ("Send fee comparison", date(2026, 11, 13), "seller")
    assert task.evidence[0].segment_ids == [4] and task.evidence[0].quote == "I'll send you the fee comparison by Friday."
    assert {"pricing_discussed", "model_flag"} <= set(result.flags) and result.overall_confidence <= 0.95
    (usage,) = out.usage
    assert (usage.service, usage.provider, usage.model, usage.stage) == ("llm", "fake", "fake-llm", Stage.extract)
    assert usage.tokens_in > 0 and usage.tokens_out > 0 and usage.conversation_id == pb_ctx.conversation.meta.conversation_id
    assert llm.calls[0].schema_name == "crm_call_note" and llm.calls[0].temperature == 0.0


@pytest.mark.parametrize("pick", ["SYN-999999", "SYN-000002", "SYN-000005"])
async def test_a_product_the_rules_did_not_establish_is_ignored(rules_extractor, taxonomy, templates, pick):
    ctx = make_context(NO_CLASS, templates["pb_institutional"])
    answer = [{"mention_id": "m1", "chosen_product_id": pick, "mention_type": "interest", "confidence": 0.99}]
    _, extractor = llm_extractor(rules_extractor, taxonomy, products=answer)
    (mention,) = (await extractor.extract(ctx)).result.products
    assert mention.chosen_product_id is None and mention.needs_review and "share_class_not_stated" in mention.reasons
    assert mention.mention_type.value == "interest"                                  # the stance the model gave is accepted


async def test_a_due_date_that_contradicts_the_wording_is_corrected_to_the_rules_date(rules_extractor, taxonomy, pb_ctx):
    wrong = [task_answer("Send fee comparison", due="2026-11-20", due_text="by Friday")]
    _, extractor = llm_extractor(rules_extractor, taxonomy, mode="llm", tasks=wrong)
    result = (await extractor.extract(pb_ctx)).result
    assert result.tasks[0].due_date == date(2026, 11, 13) and "task_date_corrected" in result.flags
    right = [task_answer("Send fee comparison", due="2026-11-13", due_text="by Friday")]
    _, extractor = llm_extractor(rules_extractor, taxonomy, mode="llm", tasks=right)
    assert "task_date_corrected" not in (await extractor.extract(pb_ctx)).result.flags


UNUSABLE_TASKS = [task_answer("Customer sends docs", "customer", (3,)), task_answer("No evidence", segs=()),
                  task_answer("Unknown segment", segs=(99,)), task_answer("Omitted segment", segs=(6,))]


async def test_customer_owned_or_unsupported_tasks_are_dropped_and_hybrid_falls_back_to_rules(rules_extractor, taxonomy,
                                                                                               pb_ctx):
    _, llm_only = llm_extractor(rules_extractor, taxonomy, mode="llm", tasks=UNUSABLE_TASKS)
    assert (await llm_only.extract(pb_ctx)).result.tasks == []
    _, hybrid = llm_extractor(rules_extractor, taxonomy, mode="hybrid", tasks=UNUSABLE_TASKS)
    expected = [t.title for t in (await rules_extractor.extract(pb_ctx)).result.tasks]
    assert expected and [t.title for t in (await hybrid.extract(pb_ctx)).result.tasks] == expected


async def test_unknown_topics_and_topics_without_valid_segments_are_ignored(rules_extractor, taxonomy, pb_ctx):
    topics = [{"topic_id": "made_up", "confidence": 0.9, "segment_ids": [3]},
              {"topic_id": "tax_efficiency", "confidence": 0.9, "segment_ids": [99]},
              {"topic_id": "tax_efficiency", "confidence": 0.7, "segment_ids": [9, 6]}]
    _, extractor = llm_extractor(rules_extractor, taxonomy, mode="llm", topics=topics)
    (tag,) = (await extractor.extract(pb_ctx)).result.topics
    assert tag.topic_id == "tax_efficiency" and tag.evidence[0].segment_ids == [9]


async def test_llm_mode_falls_back_to_the_rule_topics_when_the_model_returns_none_usable(rules_extractor, taxonomy, pb_ctx):
    _, extractor = llm_extractor(rules_extractor, taxonomy, mode="llm", topics=[{"topic_id": "made_up", "confidence": 1, "segment_ids": [3]}])
    expected = [t.topic_id for t in (await rules_extractor.extract(pb_ctx)).result.topics]
    assert expected and [t.topic_id for t in (await extractor.extract(pb_ctx)).result.topics] == expected


async def test_hybrid_keeps_the_rule_topics_and_only_adds_strongly_supported_model_topics(rules_extractor, taxonomy, pb_ctx):
    rule_topics = [t.topic_id for t in (await rules_extractor.extract(pb_ctx)).result.topics]
    assert "market_outlook" not in rule_topics and "competitor_comparison" not in rule_topics
    weak = {"topic_id": "market_outlook", "confidence": 1.0, "segment_ids": [1, 3]}                   # 2 segments: not enough
    strong = {"topic_id": "competitor_comparison", "confidence": 1.0, "segment_ids": [1, 2, 3, 9, 10]}  # 5 segments
    confirmed = {"topic_id": rule_topics[0], "confidence": 1.0, "segment_ids": [1]}
    _, extractor = llm_extractor(rules_extractor, taxonomy, topics=[confirmed, weak, strong])
    result = (await extractor.extract(pb_ctx)).result
    got = [t.topic_id for t in result.topics]
    assert got[:len(rule_topics)] == rule_topics                                                      # the rules decide the base set
    assert "competitor_comparison" in got and "market_outlook" not in got
    assert len(got) <= 5
    kept = next(t for t in result.topics if t.topic_id == rule_topics[0])
    assert kept.confidence < 1.0                                                                        # the rules' confidence, not the model's 1.0


async def test_hybrid_ignores_the_model_topic_list_when_it_is_empty(rules_extractor, taxonomy, pb_ctx):
    _, extractor = llm_extractor(rules_extractor, taxonomy, topics=[])
    expected = [t.topic_id for t in (await rules_extractor.extract(pb_ctx)).result.topics]
    assert [t.topic_id for t in (await extractor.extract(pb_ctx)).result.topics] == expected


async def test_a_section_missing_from_the_model_output_is_present_but_empty(rules_extractor, taxonomy, pb_ctx, templates):
    only_overview = [{"key": "overview", "text": "Only the overview.", "items": []}]
    _, extractor = llm_extractor(rules_extractor, taxonomy, sections=only_overview)
    result = (await extractor.extract(pb_ctx)).result
    assert [s.key for s in result.sections] == [s.key for s in templates["pb_institutional"].sections]
    assert result.sections[0].text == "Only the overview."
    assert all(s.text is None and s.items == [] for s in result.sections[1:])


async def test_the_prompt_carries_only_usable_segments_and_business_line_options(rules_extractor, taxonomy, pb_ctx, retail_ctx):
    llm, hybrid = llm_extractor(rules_extractor, taxonomy)
    await hybrid.extract(pb_ctx)
    prompt = llm.calls[0].user
    payload = json.loads(prompt)
    assert [t["id"] for t in payload["transcript"]] == [1, 2, 3, 4, 5, 7, 8, 9, 10]          # 0 small talk and 6 omitted are gone
    assert "cancer" not in prompt and "4111" not in prompt and "[PAYMENT_CARD]" in prompt
    assert "suggestions" in payload and "relationship_facts" not in payload                  # hybrid hints; template without them
    llm, llm_only = llm_extractor(rules_extractor, taxonomy, mode="llm")
    await llm_only.extract(retail_ctx)
    retail = json.loads(llm.calls[0].user)
    assert "suggestions" not in retail and retail["relationship_facts"] == ["Supports the Seattle Seahawks"]
    assert "due_diligence_approval" not in {t["topic_id"] for t in retail["topic_options"]}  # not offered for retail


def test_extractor_factory_and_mode_validation(resolver, topic_engine, taxonomy, rules_extractor):
    args = {"resolver": resolver, "topics": topic_engine, "tasks": RulesTaskEngine(), "summarizer": RulesSummaryEngine(),
            "taxonomy": taxonomy}
    assert list(build_extractors(**args)) == ["rules"]
    with_llm = build_extractors(**args, llm=FakeLlm({}))
    assert {key: extractor.name for key, extractor in with_llm.items()} == {"rules": "rules", "hybrid": "hybrid", "llm": "llm"}
    with pytest.raises(ValueError):
        LlmExtractor(FakeLlm({}), rules_extractor, taxonomy, mode="rules")

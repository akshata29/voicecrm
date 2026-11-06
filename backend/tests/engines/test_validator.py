"""Validator: the independent last gate - leak scrubbing, evidence, catalogue / taxonomy / template contract checks."""
from __future__ import annotations

from datetime import date

import pytest

from voicecrm.domain.enums import OwnerRole
from voicecrm.domain.extraction import Evidence, NoteSection, RelationshipFact, TaskItem, TopicTag
from voicecrm.domain.policy import NotesPolicy

from .helpers import make_context

TURNS = [
    ("s", "Good morning Priya, thanks for joining."),
    ("c", ("We are evaluating the Contoso Equity Income Fund, Advisor class, for our pension mandate, and the investment "
           "committee needs a decision by December.")),
    ("c", ("We currently hold the Contoso Equity Fund, class I, and the fees are a concern because the expense ratio "
           "looks higher than our benchmark.")),
    ("s", "I'll send you the fee comparison by Friday."),
    ("c", "I'm a huge Seahawks fan, by the way."),
    ("s", "Let me get the updated fact sheet over to you tomorrow."),
]
CARD = "4111 1111 1111 1111"


@pytest.fixture
async def clean(templates, rules_extractor):
    """(context, extraction result) for a clean call under the institutional template."""
    ctx = make_context(TURNS, templates["pb_institutional"])
    return ctx, (await rules_extractor.extract(ctx)).result


def validate(validator, result, ctx, notes=None):
    return validator.validate(result, ctx, notes=notes or NotesPolicy())


def codes(report):
    return [f.code for f in report.findings]


def evidence_for(ctx, segment_id):
    return [Evidence(segment_ids=[segment_id], quote=ctx.conversation.segments[segment_id].text)]


async def test_a_clean_result_passes_without_findings_and_is_returned_as_a_copy(validator, clean):
    ctx, result = clean
    cleaned, report = validate(validator, result, ctx)
    assert report.passed and report.findings == [] and report.leak_count == 0 and report.unsupported_claims == 0
    assert cleaned == result and cleaned is not result


async def test_planted_leaks_are_scrubbed_in_the_copy_and_reported(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.sections[1].items += [f"Client shared card {CARD} for the subscription",
                                  "Reach them at priya dot nair at northwind dot com",
                                  "The client mentioned chemotherapy last month"]
    planted.tasks[0].title = f"Send fee comparison to card {CARD}"
    planted.tasks[0].evidence[0].quote += f" Card {CARD}"
    cleaned, report = validate(validator, planted, ctx)
    dump = cleaned.model_dump_json().lower()
    for needle in ("4111", "chemotherapy", "northwind dot com"):
        assert needle not in dump
    assert "[PAYMENT_CARD]" in cleaned.tasks[0].title and "[PERSONAL_CONTACT]" in cleaned.sections[1].items[-2]
    assert cleaned.sections[1].items[-1].startswith("[omitted: ")
    assert report.leak_count >= 5 and not report.passed
    assert {"leak.payment_card", "leak.personal_contact", "leak.medical"} <= set(codes(report))
    assert CARD in planted.sections[1].items[-3]                                    # the input object is never mutated


async def test_categories_the_policy_does_not_omit_are_not_scrubbed(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.sections[1].items.append("Reach them at priya dot nair at northwind dot com")
    cleaned, report = validate(validator, planted, ctx, NotesPolicy(omit_categories=["payment_card"]))
    assert report.leak_count == 0 and report.passed and "northwind dot com" in cleaned.sections[1].items[-1]


async def test_unknown_chosen_product_is_cleared_and_sent_to_review(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.products[0].chosen_product_id = "SYN-999999"
    cleaned, report = validate(validator, planted, ctx)
    mention = cleaned.products[0]
    assert mention.chosen_product_id is None and mention.needs_review and "unknown_product" in mention.reasons
    assert "catalogue.unknown_product" in codes(report) and not report.passed


async def test_stated_share_class_that_contradicts_the_chosen_product_is_cleared(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.products[0].chosen_product_id, planted.products[0].share_class_stated = "SYN-000001", "C"   # SYN-000001 is class A
    cleaned, report = validate(validator, planted, ctx)
    assert cleaned.products[0].chosen_product_id is None and cleaned.products[0].needs_review
    assert "catalogue.share_class_mismatch" in codes(report) and not report.passed


async def test_candidates_outside_the_catalogue_are_dropped_with_a_warning(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.products[0].candidates[0].product_id = "SYN-999999"
    cleaned, report = validate(validator, planted, ctx)
    assert len(cleaned.products[0].candidates) == len(result.products[0].candidates) - 1
    assert codes(report) == ["catalogue.unknown_candidate"] and report.passed


async def test_unknown_topics_are_dropped_and_known_labels_are_normalised(validator, clean, taxonomy):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.topics[0].label = "Made-up label"
    planted.topics.append(TopicTag(topic_id="made_up", label="x", confidence=0.9, evidence=planted.topics[0].evidence))
    cleaned, report = validate(validator, planted, ctx)
    assert [t.topic_id for t in cleaned.topics] == [t.topic_id for t in result.topics]
    assert cleaned.topics[0].label == taxonomy.by_id()[cleaned.topics[0].topic_id].label
    assert "taxonomy.unknown_topic" in codes(report)


async def test_tasks_without_transcript_evidence_are_dropped_and_counted(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.tasks.insert(0, TaskItem(task_id="t7", title="Build a rocket", confidence=0.8, owner_role=OwnerRole.seller,
                                     evidence=[Evidence(segment_ids=[1], quote="we will build a rocket to the moon")]))
    planted.tasks.append(TaskItem(task_id="t8", title="No evidence at all", confidence=0.8, evidence=[]))
    cleaned, report = validate(validator, planted, ctx)
    assert [t.title for t in cleaned.tasks] == [t.title for t in result.tasks]
    assert [t.task_id for t in cleaned.tasks] == [f"t{n}" for n in range(1, len(result.tasks) + 1)]     # renumbered
    assert report.unsupported_claims == 2 and codes(report).count("evidence.missing") == 2


async def test_customer_tasks_are_dropped_and_implausible_due_dates_cleared(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.tasks.append(TaskItem(task_id="t8", title="Customer sends the DDQ", confidence=0.8, owner_role=OwnerRole.customer,
                                  evidence=evidence_for(ctx, 1)))
    planted.tasks.append(TaskItem(task_id="t9", title="Stale item", confidence=0.8, due_date=date(2020, 1, 1), due_basis="stated",
                                  evidence=evidence_for(ctx, 3)))
    cleaned, report = validate(validator, planted, ctx)
    assert "Customer sends the DDQ" not in [t.title for t in cleaned.tasks]
    stale = next(t for t in cleaned.tasks if t.title == "Stale item")
    assert stale.due_date is None and stale.due_basis == "none"
    assert {"task.customer_commitment", "task.due_out_of_range"} <= set(codes(report)) and report.passed


async def test_product_without_evidence_is_kept_but_sent_to_review(validator, clean):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.products[0].evidence = [Evidence(segment_ids=[1], quote="something nobody said about the fund")]
    cleaned, report = validate(validator, planted, ctx)
    assert cleaned.products[0].needs_review and "no_evidence" in cleaned.products[0].reasons
    assert report.unsupported_claims == 1 and report.passed


async def test_relationship_facts_are_removed_unless_the_template_allows_them(validator, clean, templates, rules_extractor):
    ctx, result = clean
    planted = result.model_copy(deep=True)
    planted.relationship_facts = [RelationshipFact(text="Supports the Seattle Seahawks", evidence=evidence_for(ctx, 4))]
    cleaned, report = validate(validator, planted, ctx)                             # pb_institutional: no relationship notes
    assert cleaned.relationship_facts == [] and "policy.relationship_not_allowed" in codes(report)
    retail_ctx = make_context(TURNS, templates["retail_advisor"], business_line="retail")
    retail = (await rules_extractor.extract(retail_ctx)).result
    kept, retail_report = validate(validator, retail, retail_ctx)
    assert [f.text for f in kept.relationship_facts] == ["Supports the Seattle Seahawks"] and retail_report.passed


async def test_missing_and_over_long_sections_are_repaired_in_template_order(validator, clean, templates):
    ctx, result = clean
    template = templates["pb_institutional"]
    planted = result.model_copy(deep=True)
    planted.sections = [planted.sections[3], planted.sections[0],
                        NoteSection(key="client_context", title="x", items=[f"item {n}" for n in range(9)])]
    cleaned, report = validate(validator, planted, ctx)
    assert [s.key for s in cleaned.sections] == [s.key for s in template.sections]
    limits = {s.key: s.max_items for s in template.sections}
    assert all(len(s.items) <= limits[s.key] for s in cleaned.sections if limits[s.key] is not None)
    assert len(cleaned.sections[1].items) == limits["client_context"]
    assert {"template.missing_section", "template.too_many_items", "template.empty_required_section"} <= set(codes(report))
    assert report.passed                                                            # repairs are warnings, not errors

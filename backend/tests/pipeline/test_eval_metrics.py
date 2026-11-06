"""Scoring tests with hand-built gold labels and proposals (independent of the engines)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from voicecrm.domain.conversation import ConversationMeta, Participant
from voicecrm.domain.crm import CrmMatch, CrmRecordRef
from voicecrm.domain.enums import Channel, Engine, OwnerRole, SpeakerRole
from voicecrm.domain.extraction import (
    Evidence, ExtractionResult, ProductCandidate, ProductMention, TaskItem, TopicTag, ValidationReport,
)
from voicecrm.domain.fixtures import GoldLabels, GoldProduct, GoldTask, ScriptedCall
from voicecrm.domain.pipeline import ConversationRecord, Proposal, ReviewInfo
from voicecrm.eval.metrics import aggregate, contains_leak, score_case

FAMILY = {"P-C": "F1", "P-A": "F1", "P-ADV": "F1", "Q-C": "F2"}


def _call(gold: GoldLabels) -> ScriptedCall:
    p = Participant(id="seller", display_name="Sam", role=SpeakerRole.seller)
    return ScriptedCall(fixture_id="demo", title="Demo", description="d", channel=Channel.teams, seller=p,
                        customers=[Participant(id="cust1", display_name="Cy", role=SpeakerRole.customer)], turns=[], gold=gold)


def _record(result: ExtractionResult, note: str = "note") -> ConversationRecord:
    meta = ConversationMeta(source=Channel.teams, source_interaction_id="x",
                            seller=Participant(id="seller", display_name="Sam", role=SpeakerRole.seller))
    rec = ConversationRecord(meta=meta)
    rec.proposal = Proposal(
        template_id="pb_institutional", note_markdown=note, result=result, validation=ValidationReport(passed=True),
        crm_match=CrmMatch(account=CrmRecordRef(entity="account", id="acc-1")), review=ReviewInfo(),
        ready_at=datetime.now(timezone.utc))
    return rec


def _mention(chosen: str | None, cands: list[str], needs_review: bool = False) -> ProductMention:
    return ProductMention(
        mention_id="m1", surface="x", evidence=[Evidence(segment_ids=[1], quote="q")],
        candidates=[ProductCandidate(product_id=c, display_name=c, fund_name="F", share_class="C", score=0.9) for c in cands],
        chosen_product_id=chosen, confidence=0.9, needs_review=needs_review)


def _result(products=(), topics=(), tasks=()) -> ExtractionResult:
    return ExtractionResult(template_id="pb_institutional", sections=[], products=list(products), topics=list(topics),
                            tasks=list(tasks), overall_confidence=0.95, engine=Engine.rules)


def test_exact_share_class_and_confusion_are_distinguished():
    gold = GoldLabels(template_id="t", products=[GoldProduct(product_id="P-C")])
    ok = score_case(_call(gold), _record(_result([_mention("P-C", ["P-C", "P-A"])])), family_of=FAMILY)
    assert (ok.product_top1_correct, ok.product_top3_hits, ok.share_class_confusions) == (1, 1, 0)
    wrong = score_case(_call(gold), _record(_result([_mention("P-A", ["P-A", "P-C"])])), family_of=FAMILY)
    assert (wrong.product_top1_correct, wrong.product_top3_hits, wrong.share_class_confusions) == (0, 1, 1)
    assert wrong.product_false_tags == 1


def test_ambiguous_gold_counts_a_correct_pick_list_as_top3_only():
    gold = GoldLabels(template_id="t", products=[GoldProduct(product_id="P-C", ambiguous=True, acceptable_ids=["P-C", "P-A"])])
    case = score_case(_call(gold), _record(_result([_mention(None, ["P-A", "P-C", "P-ADV"], needs_review=True)])), family_of=FAMILY)
    assert case.product_top3_hits == 1 and case.product_top1_correct == 0 and case.product_false_tags == 0
    assert case.details["gold_nonambiguous"] == 0


def test_topics_and_tasks_with_due_dates():
    gold = GoldLabels(template_id="t", topics=["fees", "esg"],
                      tasks=[GoldTask(keywords=["send", "fee"], due_date=date(2026, 11, 13))])
    ev = [Evidence(segment_ids=[1], quote="q")]
    result = _result(
        topics=[TopicTag(topic_id="fees", label="Fees", confidence=1, evidence=ev),
                TopicTag(topic_id="liquidity", label="L", confidence=1, evidence=ev)],
        tasks=[TaskItem(task_id="t1", title="Send fee comparison", due_date=date(2026, 11, 12), confidence=1, evidence=ev,
                        owner_role=OwnerRole.seller)])
    case = score_case(_call(gold), _record(result), family_of=FAMILY)
    assert (case.topic_tp, case.topic_fp, case.topic_fn) == (1, 1, 1)
    assert (case.task_tp, case.task_fp, case.task_fn) == (0, 1, 1)   # right words, wrong date


def test_leak_detection_handles_spaced_card_numbers_and_case():
    assert contains_leak("card 4111-1111-1111-1111 ok", "4111 1111 1111 1111")
    assert contains_leak("My Wife Has Cancer", "wife has cancer")
    assert not contains_leak("clean note", "4111 1111 1111 1111")
    gold = GoldLabels(template_id="t", must_not_leak=["4111 1111 1111 1111"], must_keep=["seahawks"])
    case = score_case(_call(gold), _record(_result(), note="Customer pays with 4111111111111111. Seahawks fan"), family_of=FAMILY)
    assert case.leaks == 1 and case.kept_relationship == 1


def test_failed_conversation_scores_all_misses():
    gold = GoldLabels(template_id="t", products=[GoldProduct(product_id="P-C")], topics=["fees"],
                      tasks=[GoldTask(keywords=["send"])])
    meta = ConversationMeta(source=Channel.teams, source_interaction_id="x",
                            seller=Participant(id="seller", display_name="Sam", role=SpeakerRole.seller))
    case = score_case(_call(gold), ConversationRecord(meta=meta, error="boom"), family_of=FAMILY)
    assert (case.topic_fn, case.task_fn, case.product_top1_correct) == (1, 1, 0)


def test_aggregate_rates():
    gold = GoldLabels(template_id="t", products=[GoldProduct(product_id="P-C")], topics=["fees"])
    good = score_case(_call(gold), _record(_result([_mention("P-C", ["P-C"])],
                                                   topics=[TopicTag(topic_id="fees", label="F", confidence=1, evidence=[])])), family_of=FAMILY)
    bad = score_case(_call(gold), _record(_result([_mention("P-A", ["P-A", "P-C"])])), family_of=FAMILY)
    m = aggregate([good, bad])
    assert m.cases == 2 and m.product_top1 == 0.5 and m.product_top3 == 1.0
    assert m.topic_recall == 0.5 and m.privacy_leaks == 0

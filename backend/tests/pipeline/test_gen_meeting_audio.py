"""tools/gen_meeting_audio.py: spoken turns on one timeline with per-speaker stems, and the rules every script follows."""
from __future__ import annotations

import importlib.util
import re
import sys
from array import array
from pathlib import Path

import pytest

from voicecrm.config import REPO_ROOT

RATE = 16000


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("gen_meeting_audio", REPO_ROOT / "tools" / "gen_meeting_audio.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                        # dataclasses resolves string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def voiced(seconds: float) -> bytes:
    return b"\x10\x00" * int(RATE * seconds)               # constant non-zero 16-bit samples


def test_stems_share_the_mix_timeline_and_add_up_to_it(tool):
    turns = [("seller", voiced(1.0)), ("customer", voiced(0.5)), ("seller", voiced(0.25))]
    out = tool.assemble(turns, RATE, lead_in=0.5, tail=1.0, gap=(0.2, 0.2))
    assert len(out.mix) == len(out.seller) == len(out.customer)
    mix, seller, customer = (array("h", track) for track in (out.mix, out.seller, out.customer))
    assert all(m == s + c for m, s, c in zip(mix, seller, customer, strict=True))
    assert all(s == 0 or c == 0 for s, c in zip(seller, customer, strict=True))          # nobody talks over anybody
    assert len(out.mix) / (2 * RATE) == pytest.approx(0.5 + 1.0 + 0.2 + 0.5 + 0.2 + 0.25 + 1.0)


def test_cues_mark_each_turn_in_order_with_the_pauses_between(tool):
    out = tool.assemble([("seller", voiced(1.0)), ("customer", voiced(2.0))], RATE, lead_in=1.5, gap=(0.6, 0.6))
    (s1, e1), (s2, e2) = out.cues
    assert (s1, e1) == pytest.approx((1.5, 2.5)) and (s2, e2) == pytest.approx((3.1, 5.1))
    seller = array("h", out.seller)
    assert not any(seller[int(RATE * 3.1):int(RATE * 5.1)])                       # the seller stem is silent during her turn


def test_pauses_are_the_same_on_every_run(tool):
    turns = [("seller", voiced(0.3)), ("customer", voiced(0.3)), ("seller", voiced(0.3))]
    assert tool.assemble(turns, RATE).mix == tool.assemble(turns, RATE).mix
    assert tool.assemble(turns, RATE, seed=1).mix != tool.assemble(turns, RATE, seed=2).mix


def test_the_transcript_and_the_teams_style_vtt_carry_the_names_and_times(tool):
    script = tool.MeetingScript(title="T", seller=tool.Person("Aisha West", "v1"), customer=tool.Person("Dana Whitfield", "v2"),
                                turns=[tool.Turn("seller", "Hi Dana."), tool.Turn("customer", "Hi Aisha.")])
    out = tool.assemble([("seller", voiced(1.0)), ("customer", voiced(1.0))], RATE, lead_in=1.5, gap=(0.6, 0.6))
    assert "[00:01] Aisha West: Hi Dana." in tool.script_text(script, out, RATE)
    vtt = tool.script_vtt(script, out)
    assert vtt.startswith("WEBVTT") and "00:00:01.500 --> 00:00:02.500" in vtt
    assert "<v Aisha West>Hi Dana.</v>" in vtt and "<v Dana Whitfield>Hi Aisha.</v>" in vtt


def test_a_script_needs_a_speaker_and_text_on_every_turn(tool, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"title": "t", "seller": {"name": "a", "voice": "v"}, "customer": {"name": "b", "voice": "w"}, '
                   '"turns": [{"speaker": "narrator", "text": "hello"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="speaker"):
        tool.MeetingScript.load(bad)


SCRIPTS = sorted((REPO_ROOT / "data" / "meeting").glob("*.json"))
COMMIT_VERBS = r"(?:also\s+|just\s+)?(?:send|share|forward|email|schedule|set\s?up|arrange|book|prepare|draft|put\s+together|pull\s+together|follow\s?up|circle\s+back)"
PROMISE = re.compile(rf"\b(?:i'll|i will|i can|we'll|we will|let me)\s+{COMMIT_VERBS}\b"
                     r"|\b(?:le|te|les)\s+(?:envío|envio|mando|enviaré|mandaré)\b|\bvoy a\s+(?:enviar|mandar|preparar)\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def scripts(tool):
    return tool.load_scripts()


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_every_script_survives_being_played_into_one_microphone(tool, path):
    """One microphone means one speaker label, so a customer promise would be filed as a seller task."""
    script = tool.MeetingScript.load(path)
    customer_lines = [t.text for t in script.turns if t.speaker == "customer"]
    assert customer_lines and not [line for line in customer_lines if PROMISE.search(line)]
    everything = " ".join(t.text for t in script.turns)
    assert not re.search(r"\b(today|tonight|tomorrow|hoy|esta noche)\b", everything, re.IGNORECASE)     # call dates are UTC
    assert {t.speaker for t in script.turns} == {"seller", "customer"} and script.turns[0].speaker == "seller"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_no_script_says_class_our_which_speech_hears_as_a_share_class(tool, path):
    """Azure Speech turned "the share class our plan can hold" into "the share Class R plan", a phantom product mention."""
    everything = " ".join(t.text for t in tool.MeetingScript.load(path).turns)
    assert not re.search(r"\bclass (our|are)\b", everything, re.IGNORECASE)


def test_the_scripts_have_gold_labels_a_scenario_line_and_a_unique_play_order(tool, scripts):
    orders = [script.order for _, script in scripts]
    assert orders == sorted(set(orders)) and scripts[0][0].stem == "class-c-call"
    for path, script in scripts:
        assert script.gold is not None and script.gold.products and script.gold.topics, path.stem
        assert script.shows and script.scenario, path.stem
        tool.to_scripted_call(script, path.stem)


def test_one_microphone_labels_every_line_as_the_host_and_two_speakers_keeps_both(tool, scripts):
    path, script = scripts[0]
    one = tool.to_scripted_call(script, path.stem)
    assert {t.speaker for t in one.turns} == {"seller"} and len(one.turns) == len(script.turns)
    assert {t.speaker for t in tool.to_scripted_call(script, path.stem, one_microphone=False).turns} == {"seller", "customer"}
    assert one.started_at == tool.REFERENCE_START


def test_the_class_c_call_corrects_the_share_class_out_loud(tool):
    everything = " ".join(t.text for t in tool.MeetingScript.load(tool.DEFAULT_SCRIPT).turns)
    assert re.search(r"\bnot the Class A\b", everything) and "Class C" in everything


def test_a_script_can_be_named_instead_of_pathed(tool):
    named = tool.SCRIPT_DIR / "competitor-call.json"
    assert tool.resolve_script(Path("competitor-call")) == named
    assert tool.resolve_script(named) == named
    assert tool.resolve_script(Path("no-such-script")) == Path("no-such-script")


def test_the_play_list_follows_the_play_order_and_shows_which_recordings_exist(tool, scripts, tmp_path):
    (tmp_path / "class-c-call.wav").write_bytes(tool.wav_bytes(voiced(2.0), RATE))
    rows = [line.split() for line in tool.playlist(scripts, tmp_path).splitlines()[1:]]
    assert [row[1] for row in rows] == [path.stem for path, _ in scripts]
    assert rows[0][2] == "00:02" and all(row[2] == "-" for row in rows[1:])

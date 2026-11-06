"""tools/env_set.py: edits .env in place without printing secrets, keeping comments, other lines and line endings."""
from __future__ import annotations

import importlib.util

import pytest

from voicecrm.config import REPO_ROOT


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("env_set", REPO_ROOT / "tools" / "env_set.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_existing_setting_is_replaced_in_place_and_a_missing_one_is_appended(tool):
    text = "# adapters\nVOICECRM_A=old\nVOICECRM_B=keep\n"
    assert tool.apply(text, {"VOICECRM_A": "new", "VOICECRM_C": "added"}) == (
        "# adapters\nVOICECRM_A=new\nVOICECRM_B=keep\nVOICECRM_C=added\n")


def test_windows_line_endings_survive_and_a_missing_final_newline_is_added(tool):
    assert tool.apply("A=1\r\nB=2", {"B": "3", "C": "4"}) == "A=1\r\nB=3\r\nC=4\r\n"


def test_only_the_first_live_assignment_changes_and_comments_are_left_alone(tool):
    text = "#VOICECRM_A=commented\nVOICECRM_A=first\nVOICECRM_A=second\n"
    assert tool.apply(text, {"VOICECRM_A": "x"}) == "#VOICECRM_A=commented\nVOICECRM_A=x\nVOICECRM_A=second\n"


def test_values_that_a_dotenv_parser_would_misread_are_quoted(tool):
    assert tool.render("plain~value._-1") == "plain~value._-1"
    assert tool.render("has space") == '"has space"'
    assert tool.render("a#b") == '"a#b"'
    assert tool.render('say "hi"') == '"say \\"hi\\""'
    assert tool.render("") == ""


def test_the_command_line_sets_values_and_never_prints_a_secret(tool, tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("VOICECRM_AZURE_AUTH=key\nVOICECRM_TEAMS_CLIENT_SECRET=old-secret\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("s3cret~value\n"))
    code = tool.main(["--file", str(env), "--backup", ".env.before", "VOICECRM_AZURE_AUTH=entra", "--stdin",
                      "VOICECRM_TEAMS_CLIENT_SECRET"])
    out = capsys.readouterr().out
    assert code == 0
    assert env.read_text(encoding="utf-8") == "VOICECRM_AZURE_AUTH=entra\nVOICECRM_TEAMS_CLIENT_SECRET=s3cret~value\n"
    assert "s3cret" not in out and "old-secret" not in out and "<secret, 12 characters>" in out
    assert (tmp_path / ".env.before").read_text(encoding="utf-8") == "VOICECRM_AZURE_AUTH=key\nVOICECRM_TEAMS_CLIENT_SECRET=old-secret\n"


def test_an_existing_backup_is_never_overwritten(tool, tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.before").write_text("the first backup\n", encoding="utf-8")
    assert tool.main(["--file", str(env), "--backup", ".env.before", "A=2"]) == 0
    assert (tmp_path / ".env.before").read_text(encoding="utf-8") == "the first backup\n"
    assert env.read_text(encoding="utf-8") == "A=2\n"


def test_empty_standard_input_changes_nothing(tool, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("  \n"))
    assert tool.main(["--file", str(env), "--stdin", "A"]) == 1
    assert env.read_text(encoding="utf-8") == "A=1\n"


@pytest.mark.parametrize("argv", [["lowercase=1"], ["NOEQUALS"], ["A=line\nbreak"], []])
def test_bad_arguments_are_refused_without_touching_the_file(tool, tmp_path, argv):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        tool.main(["--file", str(env), *argv])
    assert env.read_text(encoding="utf-8") == "A=1\n"

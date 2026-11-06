"""tools/check_public.py: the pre-push gate reports what would be published without ever echoing it."""
from __future__ import annotations

import importlib.util
import sys

import pytest

from voicecrm.config import REPO_ROOT

RULES = "# comment\n\nzebra-corp: zebra|stripes\nMixedCase!: \\bACME\\b\n"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("check_public", REPO_ROOT / "tools" / "check_public.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                        # dataclasses resolves string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_rules_skip_comments_and_honour_the_case_flag(tool):
    insensitive, sensitive = tool.load_rules(RULES)
    assert (insensitive.label, sensitive.label) == ("zebra-corp", "MixedCase")
    assert insensitive.pattern.search("A ZEBRA appears")
    assert sensitive.pattern.search("an ACME part") and not sensitive.pattern.search("an acme part")


def test_a_malformed_rule_is_rejected(tool):
    with pytest.raises(ValueError, match="label: regex"):
        tool.load_rules("no separator here")


def test_findings_carry_the_label_file_and_line_but_never_the_matched_text(tool):
    found = tool.scan_text("docs/a.md", "fine\nthe zebra deal\nfine again\n", tool.load_rules(RULES))
    assert found == [tool.Finding("content", "zebra-corp", "docs/a.md", 2)]
    assert not any(hasattr(f, "text") for f in found)


def test_file_names_are_checked_too(tool):
    rules = tool.load_rules(RULES)
    assert tool.scan_name("data/zebra-call.json", rules) == [tool.Finding("name", "zebra-corp", "data/zebra-call.json")]
    assert tool.scan_name("data/calls/ok.json", rules) == []


@pytest.mark.parametrize("path, flagged", [
    (".env", True), (".env.local", True), (".env.backup", True), (".env.example", False), ("frontend/.env.mock", False),
    ("deploy/server.pem", True),
    ("keys/id.key", True), (".state/db.sqlite", True), ("docs/notes.md", False)])
def test_sensitive_file_names(tool, path, flagged):
    assert bool(tool.scan_name(path, [])) is flagged


def test_secret_shapes_are_found_but_jwt_shaped_fixtures_are_allowed_in_tests(tool):
    key = "-----BEGIN " + "RSA PRIVATE KEY-----"
    aws = "AKIA" + "ABCDEFGHIJKLMNOP"
    jwt = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "c2lnbmF0dXJlLXZhbHVl"])
    assert [f.label for f in tool.scan_text("app/x.py", f"{key}\nid = '{aws}'\n", [])] == ["private key", "AWS access key"]
    assert [f.label for f in tool.scan_text("app/x.py", f"t = '{jwt}'", [])] == ["JWT"]
    assert tool.scan_text("backend/tests/test_x.py", f"t = '{jwt}'", []) == []
    assert [f.label for f in tool.scan_text("backend/tests/test_x.py", key, [])] == ["private key"]


def test_internal_hosts_and_mailboxes_are_flagged(tool):
    text = "see https://example.sharepoint.com/x and mail someone@microsoft.com"
    assert [f.label for f in tool.scan_text("docs/a.md", text, [])] == ["internal host or mailbox"]

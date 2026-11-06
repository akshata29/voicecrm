"""Pre-push gate for the public repository: nothing customer-, tenant- or secret-like may be in the files git would publish.

    .venv\\Scripts\\python.exe tools\\check_public.py        (or scripts\\check_public.bat)

Scans every file git tracks plus every untracked file that is not ignored, i.e. what the next commit and push would contain.
Read-only. Findings name the rule, the file and the line, never the matched text. Exit code 1 when anything is found.

  1. Your own deny-list, `.deny-terms.local` at the repo root (git-ignored, so the real names never enter the repository).
     One rule per line, `label: regex`, case-insensitive; `label!: regex` is case-sensitive; `#` starts a comment.
  2. Generic secret shapes (private keys, cloud keys, connection strings, Entra client secrets, tokens, JWTs outside tests)
     and internal hosts or mailboxes (package feeds, SharePoint, @microsoft.com).
  3. File names: .env files other than .env.example, key and certificate files, and state folders.

Binary files (screenshots, diagrams) are only checked by name: look at those by eye after regenerating them.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENY_FILE = ROOT / ".deny-terms.local"
TEST_DIR = "backend/tests/"
SELF = {"tools/check_public.py", "backend/tests/pipeline/test_check_public.py"}      # they necessarily spell out the patterns

# (label, regex, also scanned in tests): test fixtures legitimately carry fake JWT-shaped tokens
SECRET_SHAPES: list[tuple[str, str, bool]] = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", True),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{30,}", True),
    ("AWS access key", r"AKIA[0-9A-Z]{16}", True),
    ("Slack token", r"xox[baprs]-[A-Za-z0-9-]{10,}", True),
    ("connection-string key", r"(?:AccountKey|SharedAccessKey|SharedAccessSignature)\s*=\s*[A-Za-z0-9+/=%]{16,}", True),
    ("Entra client secret", r"\b[A-Za-z0-9_.-]{3}8Q~[A-Za-z0-9_.~-]{20,}", True),
    ("JWT", r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}", False),
    ("internal host or mailbox", r"pkgs\.visualstudio\.com|packagefeedproxy|sharepoint\.com|[\w.+-]+@microsoft\.com", True),
]
SENSITIVE_NAME = re.compile(r"(^|/)\.env($|\.(?!(?:example|mock)$))|\.(pem|key|pfx|p12|jks)$|(^|/)\.state[^/]*/", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Finding:
    kind: str          # "name" | "content"
    label: str
    path: str
    line: int = 0


def load_rules(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, pattern = line.partition(":")
        if not sep or not pattern.strip():
            raise ValueError(f"deny-list line must be 'label: regex': {raw!r}")
        rules.append(Rule(label.strip().rstrip("!").strip(), re.compile(pattern.strip(), 0 if label.strip().endswith("!") else re.IGNORECASE)))
    return rules


def scan_name(path: str, rules: list[Rule]) -> list[Finding]:
    found = [Finding("name", rule.label, path) for rule in rules if rule.pattern.search(path)]
    if SENSITIVE_NAME.search(path):
        found.append(Finding("name", "sensitive file name", path))
    return found


def scan_text(path: str, text: str, rules: list[Rule]) -> list[Finding]:
    """One finding per rule and line; the matched text is never part of a finding."""
    shapes = [(label, re.compile(rx)) for label, rx, in_tests in SECRET_SHAPES if in_tests or not path.startswith(TEST_DIR)]
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            if rule.pattern.search(line):
                found.append(Finding("content", rule.label, path, number))
        for label, pattern in shapes:
            if pattern.search(line):
                found.append(Finding("content", label, path, number))
    return found


def candidate_files() -> list[str]:
    def git(*args: str) -> set[bytes]:
        return set(subprocess.run(["git", *args, "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")) - {b""}

    return sorted(name.decode("utf-8", "replace") for name in git("ls-files") | git("ls-files", "-o", "--exclude-standard"))


def main() -> int:
    rules = load_rules(DENY_FILE.read_text(encoding="utf-8")) if DENY_FILE.exists() else []
    if not rules:
        print(f"note: {DENY_FILE.name} is missing or empty, so only the generic checks ran")
    findings: list[Finding] = []
    scanned = binary = 0
    for rel in candidate_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        findings += scan_name(rel, rules)
        if rel in SELF:
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            binary += 1
            continue
        scanned += 1
        findings += scan_text(rel, raw.decode("utf-8", "replace"), rules)
    for f in findings:
        print(f"  {f.label:<26} {f.path}" + (f":{f.line}" if f.line else "  (file name)"))
    state = f"{len(findings)} finding(s)" if findings else "clean"
    print(f"public check: {state} - {scanned} text files scanned, {binary} binary files checked by name only, {len(rules)} deny rules")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

"""Sets values in the repo's .env without ever printing them, so secrets can go from `az` straight into the file.

    az ad app credential reset --id <app id> --query password -o tsv | python tools\\env_set.py --stdin VOICECRM_TEAMS_CLIENT_SECRET
    python tools\\env_set.py VOICECRM_AZURE_AUTH=entra VOICECRM_FOUNDRY_DEPLOYMENT=chat54mini
    python tools\\env_set.py --backup .env.previous VOICECRM_SPEECH_KEY=

KEY=VALUE arguments set plain settings (their values are shown in the summary); `--stdin KEY` reads one value from standard input
and reports only its length. An existing assignment is replaced in place, a missing one is appended, and comments, other lines
and the file's line endings are kept. `--backup NAME` first copies the current file to NAME (next to it) unless NAME exists.
The write is atomic, so an interrupted run cannot leave a half-written .env. Pass `--file` to edit another dotenv file.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NEEDS_QUOTES = re.compile(r"[\s#\"'\\]")


def render(value: str) -> str:
    """The text to the right of `=`: bare when that is safe for a dotenv parser, double-quoted otherwise."""
    if not _NEEDS_QUOTES.search(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def apply(text: str, values: dict[str, str]) -> str:
    """`text` with each KEY set to its value: first live assignment replaced, missing ones appended."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)", line)
        if not match or match.group(2) not in values or match.group(2) in seen:
            continue
        key = match.group(2)
        seen.add(key)
        ending = line[len(line.rstrip("\r\n")):] or newline
        lines[index] = f"{match.group(1)}{key}{match.group(3)}{render(values[key])}{ending}"
    missing = [key for key in values if key not in seen]
    if missing:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines.extend(f"{key}={render(values[key])}{newline}" for key in missing)
    return "".join(lines)


def write_atomic(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("assignments", nargs="*", metavar="KEY=VALUE")
    parser.add_argument("--stdin", metavar="KEY", help="read this setting's value from standard input (a secret: never printed)")
    parser.add_argument("--backup", metavar="NAME", help="copy the current file to NAME (next to it) first; an existing backup is kept")
    parser.add_argument("--file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)

    values: dict[str, str] = {}
    shown: dict[str, bool] = {}
    for item in args.assignments:
        key, separator, value = item.partition("=")
        if not separator or not _NAME.match(key):
            parser.error(f"expected KEY=VALUE with an upper-case KEY, got {item.split('=')[0]!r}")
        values[key], shown[key] = value, True
    if args.stdin:
        if not _NAME.match(args.stdin):
            parser.error(f"--stdin needs an upper-case setting name, got {args.stdin!r}")
        secret = sys.stdin.read().strip()
        if not secret:
            print(f"nothing was read from standard input for {args.stdin}; the file is unchanged", file=sys.stderr)
            return 1
        values[args.stdin], shown[args.stdin] = secret, False
    if not values:
        parser.error("nothing to set")
    bad = [key for key, value in values.items() if "\n" in value or "\r" in value]
    if bad:
        parser.error(f"a value for {', '.join(bad)} contains a line break")

    path: Path = args.file
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if args.backup and path.exists():
        backup = path.with_name(args.backup)
        if backup.exists():
            print(f"backup {backup.name} already exists: kept as it is")
        else:
            shutil.copy2(path, backup)
            print(f"backup written to {backup.name}")
    write_atomic(path, apply(text, values))
    for key, value in values.items():
        if shown[key]:
            print(f"set {key}={value!r}" if value else f"set {key} (empty)")
        else:
            print(f"set {key} <secret, {len(value)} characters>")
    return 0


if __name__ == "__main__":
    sys.exit(main())

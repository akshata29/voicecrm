"""Speaks two-party sales calls with Azure neural voices, so you can play them into a live Teams meeting (scenario B).

Teams transcribes whatever reaches a participant's microphone. With nobody to play the customer, play a recording into the
host's microphone (a phone speaker next to the laptop works) and Teams transcribes it as if the host had said it.

    cd backend
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_meeting_audio.py                    every script in data/meeting (skips what exists)
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_meeting_audio.py --list             the scripts, in play order, and what exists
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_meeting_audio.py ..\\data\\meeting\\privacy-call.json --stems --force

Writes to data/audio/meeting (git-ignored): <name>.wav (both voices, mono), <name>.txt (the script with times) and <name>.vtt
(the same call as a Teams-style transcript, two speakers, for Sources -> Upload when Teams is not an option). `--stems` also
writes <name>.seller.wav and <name>.customer.wav, each voice alone on the same timeline, for two devices into two participants.

A script is a small JSON file (see data/meeting): the two voices, the turns, a one-line "shows", and a "gold" block (the
same shape as data/calls/*.json) saying what the pipeline should extract. `scripts\\rehearse_audio.bat --meeting` scores the
recordings against it. Everything played through one microphone is attributed to one speaker, so a script keeps every
commitment on the seller's side (see the "notes" in each script). Costs a few cents; needs the Speech settings in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import random
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent / "backend"))
sys.path.insert(0, str(TOOLS))

from gen_call_audio import read_pcm  # noqa: E402
from voicecrm.adapters.transcription.azure_tts import WAV_16K, AzureSpeechSynthesizer  # noqa: E402
from voicecrm.config import get_settings  # noqa: E402
from voicecrm.domain.conversation import Participant  # noqa: E402
from voicecrm.domain.enums import Channel, Direction, SpeakerRole  # noqa: E402
from voicecrm.domain.fixtures import GoldLabels, ScriptedCall, ScriptedTurn  # noqa: E402

WAV_24K = "riff-24khz-16bit-mono-pcm"
SCRIPT_DIR = TOOLS.parent / "data" / "meeting"
DEFAULT_SCRIPT = SCRIPT_DIR / "class-c-call.json"

# The people in every call. The customer is the contact the demo CRM already holds for the invited guest's address
# (see scripts\crm_add.bat), and the seller is the Teams host; both are overridable per script.
SELLER_EMAIL = "aisha.west@contoso.example"
CUSTOMER_EMAIL = "dana.whitfield@harborview-pension.example"      # the seed contact, so scoring matches Harborview by e-mail
CUSTOMER_PHONE = "+1 206 555 0111"
CUSTOMER_ORG = "Harborview Pension Fund"
REFERENCE_START = "2026-11-06T15:30:00Z"      # a Friday: the anchor for "by Monday" when a script is scored

Speaker = Literal["seller", "customer"]


@dataclass(frozen=True)
class Person:
    name: str
    voice: str
    email: str | None = None
    phone: str | None = None
    org: str | None = None


@dataclass(frozen=True)
class Turn:
    speaker: Speaker
    text: str


@dataclass(frozen=True)
class MeetingScript:
    title: str
    seller: Person
    customer: Person
    turns: list[Turn]
    scenario: str = ""                    # short name of what the call is about, e.g. "ambiguous-fund"
    shows: str = ""                       # one line: the capability the call demonstrates
    notes: str = ""
    order: int = 100                      # position in the recommended play order
    language: str = "en-US"
    call_type: str | None = None
    started_at: str = REFERENCE_START     # anchor for relative dates ("by Monday") when the call is scored
    gold: GoldLabels | None = None        # what the pipeline should extract; same shape as data/calls/*.json "gold"

    @classmethod
    def load(cls, path: Path) -> MeetingScript:
        raw = json.loads(path.read_text(encoding="utf-8"))
        turns = [Turn(speaker=t["speaker"], text=t["text"].strip()) for t in raw["turns"]]
        if not turns or any(t.speaker not in ("seller", "customer") or not t.text for t in turns):
            raise ValueError(f"{path.name}: every turn needs a speaker (seller or customer) and some text")
        return cls(
            title=raw["title"], turns=turns,
            seller=Person(**{"email": SELLER_EMAIL, "org": "Contoso Woodgrove", **raw["seller"]}),
            customer=Person(**{"email": CUSTOMER_EMAIL, "phone": CUSTOMER_PHONE, "org": CUSTOMER_ORG, **raw["customer"]}),
            scenario=raw.get("scenario") or path.stem, shows=raw.get("shows", ""), notes=raw.get("notes", ""),
            order=int(raw.get("order", 100)), language=raw.get("language", "en-US"), call_type=raw.get("call_type"),
            started_at=raw.get("started_at", REFERENCE_START),
            gold=GoldLabels.model_validate(raw["gold"]) if raw.get("gold") else None)

    def person(self, speaker: Speaker) -> Person:
        return self.seller if speaker == "seller" else self.customer


def load_scripts(directory: Path = SCRIPT_DIR) -> list[tuple[Path, MeetingScript]]:
    """Every call script in the folder, in play order."""
    loaded = [(path, MeetingScript.load(path)) for path in sorted(directory.glob("*.json"))]
    return sorted(loaded, key=lambda item: (item[1].order, item[0].stem))


def to_scripted_call(script: MeetingScript, fixture_id: str, *, one_microphone: bool = True) -> ScriptedCall:
    """The script as a call the pipeline and the scorer understand (the shape of data/calls/*.json).

    With one microphone (the default) every turn comes from the host, which is how Teams labels audio played into a single
    device; pass False for the two-speaker version (two devices, or the .vtt fallback).
    """
    if script.gold is None:
        raise ValueError(f"{fixture_id}: the script has no gold labels")
    seller = Participant(id="seller", display_name=script.seller.name, email=script.seller.email, role=SpeakerRole.seller,
                         org=script.seller.org)
    customer = Participant(id="cust1", display_name=script.customer.name, email=script.customer.email,
                           phone=script.customer.phone, role=SpeakerRole.customer, org=script.customer.org)
    words = sum(len(turn.text.split()) for turn in script.turns)
    return ScriptedCall(
        fixture_id=fixture_id, title=script.title, description=script.shows or script.title, channel=Channel.teams,
        direction=Direction.meeting, language=script.language, call_type=script.call_type, started_at=script.started_at,
        duration_minutes=max(1, round(words / 150)), seller=seller, customers=[customer], customer_org=customer.org,
        turns=[ScriptedTurn(speaker="seller" if one_microphone else turn.speaker, text=turn.text, locale=script.language)
               for turn in script.turns],
        gold=script.gold, tags=["meeting-demo", script.scenario])


@dataclass(frozen=True)
class Rendered:
    mix: bytes                          # 16-bit mono PCM, everyone
    seller: bytes                       # the same length, only the seller's turns
    customer: bytes                     # the same length, only the customer's turns
    cues: list[tuple[float, float]]     # (start, end) seconds of each turn


def assemble(turns: list[tuple[Speaker, bytes]], rate: int, *, lead_in: float = 1.5, tail: float = 2.5,
             gap: tuple[float, float] = (0.6, 1.0), seed: int = 7) -> Rendered:
    """Places the spoken turns on one timeline with natural pauses; the stems keep each voice at the same position."""
    rng = random.Random(seed)                                   # the same pauses every run
    mix, seller, customer = bytearray(), bytearray(), bytearray()
    cues: list[tuple[float, float]] = []

    def pause(seconds: float) -> None:
        silence = bytes(2 * int(rate * seconds))
        for track in (mix, seller, customer):
            track.extend(silence)

    pause(lead_in)
    for index, (speaker, pcm) in enumerate(turns):
        if index:
            pause(rng.uniform(*gap))
        start = len(mix) / (2 * rate)
        silence = bytes(len(pcm))
        mix.extend(pcm)
        seller.extend(pcm if speaker == "seller" else silence)
        customer.extend(pcm if speaker == "customer" else silence)
        cues.append((start, len(mix) / (2 * rate)))
    pause(tail)
    return Rendered(bytes(mix), bytes(seller), bytes(customer), cues)


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def script_text(script: MeetingScript, rendered: Rendered, rate: int) -> str:
    total = len(rendered.mix) / (2 * rate)
    lines = [script.title, f"{script.seller.name} (seller) and {script.customer.name} (customer), {clock(total)}", ""]
    for turn, (start, _end) in zip(script.turns, rendered.cues, strict=True):
        lines.append(f"[{clock(start)}] {script.person(turn.speaker).name}: {turn.text}")
    return "\n".join(lines) + "\n"


def vtt_time(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}.{int((seconds - whole) * 1000):03d}"


def script_vtt(script: MeetingScript, rendered: Rendered) -> str:
    lines = ["WEBVTT", ""]
    for turn, (start, end) in zip(script.turns, rendered.cues, strict=True):
        lines += [f"{vtt_time(start)} --> {vtt_time(end)}", f"<v {script.person(turn.speaker).name}>{turn.text}</v>", ""]
    return "\n".join(lines)


async def speak_all(script: MeetingScript, synth: AzureSpeechSynthesizer, *, rate: int, concurrency: int) -> list[tuple[Speaker, bytes]]:
    fmt = WAV_24K if rate == 24000 else WAV_16K
    gate = asyncio.Semaphore(concurrency)

    async def speak(turn: Turn) -> tuple[Speaker, bytes]:
        async with gate:
            pcm, got = read_pcm(await synth.synthesize(turn.text, voice=script.person(turn.speaker).voice, output_format=fmt))
        if got != rate:
            raise ValueError(f"the service returned {got} Hz audio, expected {rate} Hz")
        return turn.speaker, pcm

    return list(await asyncio.gather(*(speak(t) for t in script.turns)))


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def playlist(scripts: list[tuple[Path, MeetingScript]], out_dir: Path) -> str:
    """The recordings in play order: what each is called, how long it is (once generated) and what it shows."""
    lines = [f"{'#':>2}  {'recording':<28} {'length':>6}  what it shows"]
    for position, (path, script) in enumerate(scripts, start=1):
        wav = out_dir / f"{path.stem}.wav"
        length = clock(wav_seconds(wav)) if wav.exists() else "-"
        lines.append(f"{position:>2}  {path.stem:<28} {length:>6}  {script.shows or script.title}")
    return "\n".join(lines)


def resolve_script(arg: Path) -> Path:
    """A script argument is a path, or just the name of one in data/meeting (competitor-call)."""
    if arg.exists():
        return arg
    named = SCRIPT_DIR / (arg.name if arg.suffix == ".json" else f"{arg.name}.json")
    return named if named.exists() else arg


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scripts", nargs="*", type=Path, help="call scripts, by path or name (default: every script in data/meeting)")
    parser.add_argument("--list", action="store_true", help="show the scripts in play order and which recordings exist, then exit")
    parser.add_argument("--out", type=Path, help="output folder (default: data/audio/meeting)")
    parser.add_argument("--rate", type=int, choices=[16000, 24000], default=24000, help="sample rate")
    parser.add_argument("--stems", action="store_true", help="also write each voice alone on the same timeline")
    parser.add_argument("--lead-in", type=float, default=1.5, help="seconds of silence before the first word")
    parser.add_argument("--concurrency", type=int, default=4, help="parallel text-to-speech requests")
    parser.add_argument("--force", action="store_true", help="overwrite files that already exist")
    args = parser.parse_args()

    settings = get_settings()
    out_dir = args.out or settings.data_dir / "audio" / "meeting"
    paths = [resolve_script(p) for p in args.scripts] or sorted(SCRIPT_DIR.glob("*.json"))
    scripts = sorted(((path, MeetingScript.load(path)) for path in paths), key=lambda item: (item[1].order, item[0].stem))
    if args.list:
        print(playlist(scripts, out_dir))
        return 0
    if not settings.speech_endpoint or (settings.azure_auth == "key" and not settings.speech_key):
        print("Speech is not configured: set VOICECRM_SPEECH_ENDPOINT (and VOICECRM_SPEECH_KEY, or use Entra) in .env", file=sys.stderr)
        return 2

    todo: list[tuple[str, MeetingScript, dict[str, Path]]] = []
    for path, script in scripts:
        name = path.stem
        targets = {"wav": out_dir / f"{name}.wav", "txt": out_dir / f"{name}.txt", "vtt": out_dir / f"{name}.vtt"}
        if args.stems:
            targets |= {"seller": out_dir / f"{name}.seller.wav", "customer": out_dir / f"{name}.customer.wav"}
        existing = [p.name for p in targets.values() if p.exists()]
        if existing and not args.force:
            print(f"{name}: already there ({', '.join(existing)}); use --force to overwrite")
            continue
        todo.append((name, script, targets))
    if not todo:
        return 0

    credential = None
    if settings.azure_auth == "entra":
        from voicecrm.adapters.credentials import build_credential

        credential = build_credential(settings.azure_tenant_id or None)
    synth = AzureSpeechSynthesizer(endpoint=settings.speech_endpoint, key=settings.speech_key or None, auth=settings.azure_auth,
                                   credential=credential)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    try:
        for name, script, targets in todo:
            started = time.perf_counter()
            try:
                spoken = await speak_all(script, synth, rate=args.rate, concurrency=args.concurrency)
            except Exception as exc:                          # one bad voice or turn should not stop the batch
                failures += 1
                print(f"{name}: FAILED: {type(exc).__name__}: {str(exc)[:200]}")
                continue
            rendered = assemble(spoken, args.rate, lead_in=args.lead_in)
            seconds = len(rendered.mix) / (2 * args.rate)
            targets["wav"].write_bytes(wav_bytes(rendered.mix, args.rate))
            targets["txt"].write_text(script_text(script, rendered, args.rate), encoding="utf-8")
            targets["vtt"].write_text(script_vtt(script, rendered), encoding="utf-8")
            if args.stems:
                targets["seller"].write_bytes(wav_bytes(rendered.seller, args.rate))
                targets["customer"].write_bytes(wav_bytes(rendered.customer, args.rate))
            characters = sum(len(t.text) for t in script.turns)
            print(f"{name}: {clock(seconds)} ({seconds:.0f} s), {len(script.turns)} turns, {characters} characters, "
                  f"{time.perf_counter() - started:.1f} s to speak")
    finally:
        await synth.aclose()
        if credential is not None:
            await credential.close()
    print(f"\nrecordings in {out_dir}\n{playlist(scripts, out_dir)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

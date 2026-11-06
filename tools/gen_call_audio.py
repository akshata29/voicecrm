"""Renders scripted calls into real speech audio with Azure text-to-speech, for live speech-to-text demos.

The scripts in data/calls are text. To demonstrate the real path - audio -> Azure Speech -> Azure Language -> model -
you need audio, so this tool speaks each call with a distinct neural voice per speaker (Spanish turns get Spanish
voices) and a natural pause between turns, then writes one WAV per call to data/audio/live (git-ignored).

    cd backend
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_call_audio.py                     the recommended demo set
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_call_audio.py --all               every call except the code-switching one
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_call_audio.py pb-teams-equity-class-c --layout mono --rate 8000
    ..\\.venv\\Scripts\\python.exe ..\\tools\\gen_call_audio.py --list              what exists

Layouts: "stereo" puts the seller on the left channel and the customers on the right (how contact-centre platforms such
as Genesys record, so speakers are separated by channel); "mono" mixes everyone into one channel (how Teams and Zoom
recordings arrive, so the service has to diarize). "auto" (default) uses stereo for Genesys calls and mono otherwise.
Costs a few cents per call; needs the Speech settings in .env (key or Entra).
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
from array import array
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from voicecrm.adapters.sources.recordings import find_recording  # noqa: E402
from voicecrm.adapters.transcription.azure_tts import WAV_8K, WAV_16K, AzureSpeechSynthesizer  # noqa: E402
from voicecrm.config import get_settings  # noqa: E402
from voicecrm.data_loader import load_scripted_calls  # noqa: E402
from voicecrm.domain.fixtures import ScriptedCall  # noqa: E402

DEMO_SET = [
    "pb-teams-equity-class-c", "pb-ambiguous-equity-fund", "retail-teams-relationship-privacy", "pb-teams-spanish-clase-c",
    "pb-mobile-in-person-multi-product", "pb-genesys-inbound-service", "pb-zoom-equity-income-advisor", "pb-teams-legacy-blue-chip",
]
SKIP_ALL = {"pb-teams-bilingual-code-switch"}          # language identification picks one language per file

FEMALE = {"dana", "priya", "elena", "hannah", "nora", "rachel", "lucia", "casey"}
VOICES = {
    "en-US": {"F": ["en-US-JennyNeural", "en-US-AriaNeural", "en-US-SaraNeural", "en-US-NancyNeural"],
              "M": ["en-US-GuyNeural", "en-US-DavisNeural", "en-US-TonyNeural", "en-US-JasonNeural"]},
    "en-GB": {"F": ["en-GB-SoniaNeural", "en-GB-LibbyNeural"], "M": ["en-GB-RyanNeural", "en-GB-ThomasNeural"]},
    "es-US": {"F": ["es-US-PalomaNeural"], "M": ["es-US-AlonsoNeural"]},
}


def fold(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def gender_of(display_name: str) -> str:
    return "F" if fold(display_name.split()[0]) in FEMALE else "M"


def assign_voices(call: ScriptedCall) -> dict[tuple[str, str], str]:
    """(speaker key, locale) -> voice. Speakers of the same gender get different voices; customers of a UK call speak British."""
    people = {"seller": call.seller, **{c.id: c for c in call.customers}, **{p.id: p for p in call.internal}}
    used: dict[tuple[str, str], int] = {}
    out: dict[tuple[str, str], str] = {}
    for turn in call.turns:
        key = turn.speaker_id or {"seller": "seller", "customer": call.customers[0].id,
                                  "internal": call.internal[0].id if call.internal else "seller"}[turn.speaker]
        locale = turn.locale if turn.locale in VOICES else "en-US"
        if locale == "en-US" and call.market == "UK" and key != "seller":
            locale = "en-GB"
        if (key, turn.locale) in out:
            continue
        person = people.get(key, call.seller)
        gender = gender_of(person.display_name)
        pool = VOICES[locale][gender]
        n = used.get((locale, gender), 0)
        used[(locale, gender)] = n + 1
        out[(key, turn.locale)] = pool[n % len(pool)]
    return out


def read_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16-bit mono PCM from the synthesizer")
        return w.readframes(w.getnframes()), w.getframerate()


def assemble(call: ScriptedCall, pcms: list[bytes], rate: int, layout: str) -> bytes:
    """Concatenates the spoken turns with pauses; stereo keeps the seller side on the left channel, customers on the right."""
    rng = random.Random(call.fixture_id)                      # same pauses every run
    left, right = bytearray(), bytearray()
    for turn, pcm in zip(call.turns, pcms, strict=True):
        seller_side = turn.speaker in ("seller", "internal")
        silence = bytes(len(pcm))
        if layout == "stereo":
            left += pcm if seller_side else silence
            right += silence if seller_side else pcm
        else:
            left += pcm
        gap = bytes(2 * int(rate * rng.uniform(0.30, 0.65)))
        left += gap
        if layout == "stereo":
            right += gap
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setsampwidth(2)
        w.setframerate(rate)
        if layout == "stereo":
            left_ch, right_ch = array("h"), array("h")
            left_ch.frombytes(bytes(left))
            right_ch.frombytes(bytes(right))
            inter = array("h", [0]) * (2 * len(left_ch))
            inter[0::2], inter[1::2] = left_ch, right_ch
            w.setnchannels(2)
            w.writeframes(inter.tobytes())
        else:
            w.setnchannels(1)
            w.writeframes(bytes(left))
    return out.getvalue()


async def render_call(synth: AzureSpeechSynthesizer, call: ScriptedCall, *, layout: str, rate: int, concurrency: int) -> tuple[bytes, dict]:
    voices = assign_voices(call)
    fmt = WAV_8K if rate == 8000 else WAV_16K
    gate = asyncio.Semaphore(concurrency)

    async def speak(turn) -> bytes:
        key = turn.speaker_id or {"seller": "seller", "customer": call.customers[0].id,
                                  "internal": call.internal[0].id if call.internal else "seller"}[turn.speaker]
        async with gate:
            return read_pcm(await synth.synthesize(turn.text, voice=voices[(key, turn.locale)], output_format=fmt))[0]

    pcms = await asyncio.gather(*(speak(t) for t in call.turns))
    wav = assemble(call, list(pcms), rate, layout)
    return wav, {"voices": {f"{k[0]}/{k[1]}": v for k, v in voices.items()}, "layout": layout, "sample_rate": rate, "turns": len(call.turns)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixtures", nargs="*", help="fixture ids (default: the demo set)")
    parser.add_argument("--all", action="store_true", help="every call except the code-switching one")
    parser.add_argument("--layout", choices=["auto", "stereo", "mono"], default="auto")
    parser.add_argument("--rate", type=int, choices=[8000, 16000], default=16000, help="sample rate (8000 = telephone quality)")
    parser.add_argument("--out", type=Path, help="output folder (default: data/audio/live)")
    parser.add_argument("--concurrency", type=int, default=6, help="parallel text-to-speech requests")
    parser.add_argument("--force", action="store_true", help="overwrite recordings that already exist")
    parser.add_argument("--list", action="store_true", help="show which calls have a recording and exit")
    args = parser.parse_args()

    settings = get_settings()
    calls = {c.fixture_id: c for c in load_scripted_calls(settings.data_dir)}
    out_dir = args.out or settings.recordings_dir
    if args.list:
        for fid, call in calls.items():
            rec = find_recording(fid, out_dir)
            print(f"{fid:<38} {call.channel.value:<8} {'%.0f s, %s ch' % (rec.seconds, rec.channels) if rec and rec.seconds else '-'}")
        return 0
    wanted = args.fixtures or ([f for f in calls if f not in SKIP_ALL] if args.all else DEMO_SET)
    unknown = [f for f in wanted if f not in calls]
    if unknown:
        print(f"unknown fixture(s): {', '.join(unknown)} (see --list)", file=sys.stderr)
        return 2
    if not settings.speech_endpoint or (settings.azure_auth == "key" and not settings.speech_key):
        print("Speech is not configured: set VOICECRM_SPEECH_ENDPOINT and VOICECRM_SPEECH_KEY (or use Entra) in .env", file=sys.stderr)
        return 2

    credential = None
    if settings.azure_auth == "entra":
        from voicecrm.adapters.credentials import build_credential

        credential = build_credential(settings.azure_tenant_id or None)
    synth = AzureSpeechSynthesizer(endpoint=settings.speech_endpoint, key=settings.speech_key or None, auth=settings.azure_auth,
                                   credential=credential)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    failures = 0
    try:
        print(f"{'call':<38} {'layout':<7} {'length':>8} {'size':>8} {'took':>7}")
        for fid in wanted:
            target = out_dir / f"{fid}.wav"
            if target.exists() and not args.force:
                print(f"{fid:<38} exists (use --force to overwrite)")
                continue
            call = calls[fid]
            layout = args.layout if args.layout != "auto" else ("stereo" if call.channel.value == "genesys" else "mono")
            started = time.perf_counter()
            try:
                wav, info = await render_call(synth, call, layout=layout, rate=args.rate, concurrency=args.concurrency)
            except Exception as exc:                          # keep going: one bad call should not stop the batch
                failures += 1
                print(f"{fid:<38} FAILED: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            target.write_bytes(wav)
            with wave.open(str(target)) as w:
                seconds = w.getnframes() / w.getframerate()
            manifest[fid] = {**info, "seconds": round(seconds, 1), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            print(f"{fid:<38} {layout:<7} {seconds:>6.0f} s {len(wav) // 1024:>6} KB {time.perf_counter() - started:>5.1f} s")
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        await synth.aclose()
        if credential is not None:
            await credential.close()
    print(f"\nrecordings in {out_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

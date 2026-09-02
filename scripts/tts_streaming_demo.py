#!/usr/bin/env python3
"""Progressive TTS quality preview.

Before committing to Phase B (file-watching + sentence-by-sentence TTS),
this script lets you HEAR what stitched-together sentences would sound
like vs the same text synthesized in one ElevenLabs call.

Produces three artifacts under /tmp/tts-demo/:
  single.mp3       — the whole text in one synth call (baseline, smooth prosody)
  progressive.mp3  — same text split into sentences, each synth'd separately,
                     then concatenated (mimics what Phase B would emit)
  parts/*.mp3      — the individual sentence files, in case you want to play
                     them with `mpv parts/*.mp3` for the most honest preview

Listen to single.mp3, then progressive.mp3. If they sound roughly the same,
Phase B audio quality is fine. If progressive sounds choppy / has obvious
gaps / changes prosody at sentence boundaries, that's the signal to
consider phrase-level chunking or ElevenLabs's streaming endpoint instead.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time

# Pull lib from the repo so we can use the same ElevenLabs helper the
# server uses. Keeps the demo honest — same code path you'll ship.
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from lib.config import load as load_config         # noqa: E402
from lib.eleven_http import synthesize_to_file     # noqa: E402


# ---- Sample texts ------------------------------------------------------
#
# Real Claude-Code-style replies. Multiple sentences, realistic flow,
# mix of declarative + interrogative + the kind of clauses that produce
# natural sentence breaks. Pick whichever fits your patience.

SAMPLES = {
    "short": (
        "I checked the failing test. The issue is a missing import in "
        "auth.py. Want me to fix it?"
    ),
    "medium": (
        "I checked the failing tests and there are three issues. "
        "First, test_auth.py expects a 401 but is getting a 403 because "
        "the recent permission refactor changed the error code. "
        "Second, the payment integration test is timing out — the test "
        "database is missing the new column. "
        "And third, the user dashboard snapshot test is showing a date "
        "mismatch. The date is hardcoded to last year. "
        "Want me to fix them one at a time, or all in one pass?"
    ),
    "long": (
        "I've finished the audit and here's what I found. "
        "The migration script is mostly fine, but it's missing an index "
        "on the new foreign key column, which will make joins slow once "
        "the table grows past a few hundred thousand rows. "
        "There's also a subtle bug in the rollback path — it tries to "
        "drop the column before dropping the index that references it, "
        "which will fail. "
        "The tests for the new code are good, but they don't cover the "
        "case where a user has been soft-deleted in the middle of a "
        "transaction. That edge case actually happens about once a month "
        "based on the logs from the last quarter. "
        "I'd recommend three changes. "
        "Add the index in the migration. "
        "Fix the rollback ordering. "
        "And write one regression test for the soft-delete race. "
        "Should I make those changes now, or do you want to review the "
        "current state first?"
    ),
}


# ---- Sentence segmentation ---------------------------------------------
#
# Simple but works for prose. Splits on punctuation+whitespace+capital,
# with a handful of common abbreviation guards. Not bulletproof — that's
# the whole point of this demo, you're SUPPOSED to hear how good or bad
# the splits are.

_ABBR = {"Dr.", "Mr.", "Mrs.", "Ms.", "etc.", "vs.", "i.e.", "e.g."}


def split_sentences(text: str) -> list[str]:
    # Replace abbreviation periods with a sentinel so the splitter ignores them.
    sentinel = "\x00"
    work = text
    for a in _ABBR:
        work = work.replace(a, a.replace(".", sentinel))

    # Split on .?! followed by whitespace.
    parts = re.split(r"(?<=[.!?])\s+", work)
    # Restore abbreviation periods.
    parts = [p.replace(sentinel, ".").strip() for p in parts if p.strip()]
    return parts


# ---- Synthesis helpers --------------------------------------------------


def synth(text: str, voice_id: str, out: pathlib.Path) -> None:
    cfg = load_config()
    api_key = cfg.eleven_key()
    if not api_key:
        sys.exit("ELEVEN_API_KEY not configured (check ~/.config/clarp/config.toml)")
    started = time.perf_counter()
    synthesize_to_file(
        text, voice_id, out,
        api_key=api_key,
        model=cfg.eleven_model,
        speed=cfg.eleven_speed,
        timeout=30.0,
    )
    elapsed = time.perf_counter() - started
    size = out.stat().st_size
    print(f"  synth ({elapsed:.2f}s, {size:,}B) — {out.name}")


def concat_with_ffmpeg(parts: list[pathlib.Path], out: pathlib.Path) -> None:
    """Use ffmpeg's concat demuxer — the correct way to stitch MP3s
    without re-encoding. Naive `cat a.mp3 b.mp3` mostly works for
    decoders but produces broken timestamps + can introduce clicks
    at boundaries; the demuxer respects frame boundaries."""
    list_file = out.with_suffix(".list.txt")
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0",
           "-i", str(list_file), "-c", "copy", str(out)]
    subprocess.run(cmd, check=True)
    list_file.unlink()


# ---- Main ---------------------------------------------------------------


DEFAULT_VOICE = "nPczCjzI2devNBz1zQrb"   # Brian / Mike — warm male


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", choices=sorted(SAMPLES.keys()), default="medium",
                    help="which sample text to render (default: medium)")
    ap.add_argument("--voice", default=DEFAULT_VOICE,
                    help=f"ElevenLabs voice id (default: {DEFAULT_VOICE})")
    ap.add_argument("--text", default=None,
                    help="custom text (overrides --sample)")
    ap.add_argument("--out", default="/tmp/tts-demo",
                    help="output directory (default: /tmp/tts-demo)")
    args = ap.parse_args()

    text = args.text or SAMPLES[args.sample]
    out_dir = pathlib.Path(args.out)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir()

    sentences = split_sentences(text)
    print(f"\nSample: {args.sample if not args.text else 'custom'}")
    print(f"Length: {len(text)} chars, {len(sentences)} sentences")
    print(f"Voice:  {args.voice}\n")

    print("Sentences as the splitter saw them:")
    for i, s in enumerate(sentences, 1):
        print(f"  {i:2d}. {s}")

    # 1) Single-shot reference.
    print(f"\n[1/2] single-shot synthesis (one ElevenLabs call)…")
    single = out_dir / "single.mp3"
    started = time.perf_counter()
    synth(text, args.voice, single)
    single_elapsed = time.perf_counter() - started

    # 2) Progressive — one synth per sentence, then concat.
    print(f"\n[2/2] progressive synthesis ({len(sentences)} ElevenLabs calls)…")
    parts: list[pathlib.Path] = []
    started = time.perf_counter()
    for i, sentence in enumerate(sentences, 1):
        p = parts_dir / f"{i:02d}.mp3"
        synth(sentence, args.voice, p)
        parts.append(p)
    progressive_synth_elapsed = time.perf_counter() - started

    print(f"\nConcatenating {len(parts)} clips with ffmpeg…")
    progressive = out_dir / "progressive.mp3"
    concat_with_ffmpeg(parts, progressive)
    print(f"  → {progressive.name} ({progressive.stat().st_size:,}B)")

    # Summary.
    print("\n" + "=" * 60)
    print("DONE. Listen and compare:")
    print(f"  Baseline (smooth):     mpv {single}")
    print(f"  Progressive (stitched): mpv {progressive}")
    print(f"  Per-sentence parts:     mpv {parts_dir}/*.mp3")
    print()
    print("Timing:")
    print(f"  Single-shot:     {single_elapsed:.2f}s end-to-end")
    print(f"  Progressive:     {progressive_synth_elapsed:.2f}s (sum of N parallel-able calls)")
    print(f"  Time-to-first-sentence: ~{progressive_synth_elapsed / max(1, len(sentences)):.2f}s avg")
    print()
    print("What to listen for in progressive.mp3:")
    print("  • Unnatural pauses between sentences (>500ms gaps)")
    print("  • Prosody resets (each sentence sounding 'fresh' rather than flowing)")
    print("  • Volume/tone jumps at sentence boundaries")
    print("  • Click/pop artifacts at the seams")
    return 0


if __name__ == "__main__":
    sys.exit(main())

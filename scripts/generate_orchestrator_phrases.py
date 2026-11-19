#!/usr/bin/env python3
"""Populate and optionally synthesize cached orchestrator phrases.

By default this records the phrase text rows only. Pass --generate to call
Cartesia and write audio files into the configured PWA audio directory.
"""
from __future__ import annotations

import argparse
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import config  # noqa: E402
from lib.cartesia_tts import synthesize as cartesia_synthesize  # noqa: E402
from lib.db import conn, now_ms  # noqa: E402
from lib.orchestrator import ORCHESTRATOR_VOICE_ID  # noqa: E402
from lib.paths import RuntimePaths  # noqa: E402
from lib.tts_engine import make_clip_filename  # noqa: E402


# The texts live with the router that speaks them (lib.orchestrator_phrases, issue 360 R76):
# a clip is recorded for exactly the sentence the router says under that key.
from lib.orchestrator_phrases import PHRASES  # noqa: E402


def upsert_phrase(*, phrase_key: str, voice_id: str, text: str,
                  session: str = "", audio_path: str = "",
                  provider: str = "cartesia", model: str = "") -> None:
    conn().execute(
        """INSERT INTO orchestrator_phrase_cache (
               phrase_key, voice_id, session, text, audio_path, provider, model,
               generated_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(phrase_key, voice_id, session) DO UPDATE SET
               text = excluded.text,
               -- A row's audio always belongs to its text: new words without new audio
               -- clear the old audio rather than pair with it (issue 360, 439 review).
               audio_path = CASE
                   WHEN excluded.audio_path <> '' THEN excluded.audio_path
                   WHEN orchestrator_phrase_cache.text = excluded.text THEN orchestrator_phrase_cache.audio_path
                   ELSE '' END,
               provider = excluded.provider,
               model = excluded.model,
               generated_at = CASE
                   WHEN excluded.audio_path <> '' THEN excluded.generated_at
                   WHEN orchestrator_phrase_cache.text = excluded.text THEN orchestrator_phrase_cache.generated_at
                   ELSE NULL END,
               updated_at = excluded.updated_at""",
        (
            phrase_key,
            voice_id,
            session,
            text,
            audio_path,
            provider,
            model,
            now_ms() if audio_path else None,
            now_ms(),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true",
                        help="call Cartesia and write mp3 files")
    parser.add_argument("--include-agents", action="store_true",
                        help="also cache generic phrases in every agent voice")
    args = parser.parse_args()

    cfg = config.load()
    audio_dir = pathlib.Path(
        pathlib.Path.home() / ".local" / "share" / "clarp" / "audio"
    )
    try:
        audio_dir = RuntimePaths.from_home(pathlib.Path.home()).audio_dir
    except Exception:
        pass
    audio_dir.mkdir(parents=True, exist_ok=True)

    voices: list[tuple[str, str, str]] = [("", ORCHESTRATOR_VOICE_ID, ORCHESTRATOR_VOICE_ID)]
    if args.include_agents:
        for agent in agents_db.list_agents():
            # Runtime cache lookup uses the app's stored voice_id, but Cartesia
            # synthesis needs the provider-specific voice for the persona.
            synth_voice_id = cfg.cartesia_voice_for(agent["persona"]) or agent["voice_id"]
            voices.append((agent["session"], agent["voice_id"], synth_voice_id))

    key = cfg.cartesia_key()
    if args.generate and not key:
        print("Cartesia key missing; rows will be recorded without audio.")
        args.generate = False

    for session, lookup_voice_id, synth_voice_id in voices:
        for phrase_key, text in PHRASES.items():
            audio_path = ""
            if args.generate:
                target = audio_dir / make_clip_filename(session or None)
                cartesia_synthesize(
                    text=text,
                    voice_id=synth_voice_id,
                    out_path=target,
                    api_key=key,
                    model=cfg.cartesia_model,
                )
                audio_path = str(target)
            upsert_phrase(
                phrase_key=phrase_key,
                voice_id=lookup_voice_id,
                session=session,
                text=text,
                audio_path=audio_path,
                model=cfg.cartesia_model,
            )
            print(f"{phrase_key} voice={lookup_voice_id} session={session or '*'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drive the REAL server-side conversation flow and print a transcript.

Exercises the production code paths:
  * `HeraldManager.ingest_clip` for every agent reply
  * `HeraldManager.on_user_text` for every user utterance
  * `HeraldManager.set_focus` / `set_awaiting` on pane changes and /send

The only mocks are the obvious external collaborators: `FakeTTSEngine`
(for herald audio) and `AudioStream` is the real one — its SSE broadcast
is what we subscribe to and render as the transcript.

    python3 scripts/simulate-server-conversation.py
"""
from __future__ import annotations

import json
import pathlib
import queue
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from lib.audio_stream import AudioStream                # noqa: E402
from lib.herald import HeraldManager                    # noqa: E402
from lib.tts_engine import FakeTTSEngine                # noqa: E402

PERSONAS = {"claude": "Mike", "rachel": "Rachel", "bella": "Bella"}
SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="claude-pwa-sim-"))
AUDIO = SCRATCH / "audio"; AUDIO.mkdir()

agents_path = SCRATCH / "agents.json"
agents_path.write_text(json.dumps({
    "claude": {"name": "Mike",   "voice_id": "V_MIKE",   "cwd": str(SCRATCH)},
    "rachel": {"name": "Rachel", "voice_id": "V_RACHEL", "cwd": str(SCRATCH)},
    "bella":  {"name": "Bella",  "voice_id": "V_BELLA",  "cwd": str(SCRATCH)},
}))

# Real server objects, no test fakes anywhere these can be swapped out.
stream = AudioStream(AUDIO)
tts = FakeTTSEngine(AUDIO)

# Tag every clip the manager generates as a herald — done before broadcast
# so the printer thread can't race past it.
herald_urls: set[str] = set()
_orig_synth = tts.synthesize
def _tagged_synth(text, voice_id, session=None):
    result = _orig_synth(text, voice_id, session=session)
    if "here, ready for an update" in text:
        herald_urls.add(str(result))
        herald_urls.add(f"/audio/{pathlib.Path(str(result)).name}")
    return result
tts.synthesize = _tagged_synth   # type: ignore[method-assign]

herald = HeraldManager(
    stream=stream, tts=tts,
    agents=lambda: json.loads(agents_path.read_text()),
)

# Subscribe BEFORE driving the conversation so we catch every event.
sub: queue.Queue = stream.subscribe()
url_text: dict[str, str] = {}     # known clip URL → its spoken text


def printer():
    while True:
        try:
            payload = sub.get(timeout=3)
        except queue.Empty:
            return
        ev = json.loads(payload)
        if ev.get("type") != "audio":
            continue
        url = ev.get("url", "")
        sid = ev.get("session") or ""
        persona = PERSONAS.get(sid, sid or "?")
        if url in herald_urls:
            print(f"{persona + ' (herald)':<18} {persona} here, ready for an update.")
        else:
            text = url_text.get(url, "(audio)")
            print(f"{persona:<18} {text}")


printer_t = threading.Thread(target=printer, daemon=True)
printer_t.start()


# ---- helpers ----

_clip_n = 0
def _agent_reply(session: str, text: str) -> None:
    """Mirror what the Stop hook + AudioStream watcher would do in
    production: hand a clip to HeraldManager. The manager decides whether
    to broadcast, hold, or herald."""
    global _clip_n
    _clip_n += 1
    url = f"/audio/{int(time.time() * 1000)}-{_clip_n}__{session}.mp3"
    url_text[url] = text
    herald.ingest_clip(session, url=url, ts=_clip_n)


def user_says(text: str) -> None:
    print(f"{'User':<18} {text}")
    herald.on_user_text(text)


def send_to(session: str, text: str) -> None:
    """the user addresses an agent — same as POST /send. Pane (focus) does NOT
    move; that only happens via an explicit /select (tapping the chip)."""
    user_says(text)
    herald.set_awaiting(session)


def look_at(session: str) -> None:
    print(f"({'looks at ' + PERSONAS[session]})")
    herald.set_focus(session)


# ---- script ----

print("=" * 70)
print("       claude-pwa SERVER-DRIVEN conversation (real HeraldManager)")
print("=" * 70 + "\n")

# Beat 1 — focused turn with Mike
look_at("claude")
send_to("claude", "Hey Mike, what is two plus two?")
_agent_reply("claude", "Four.")
time.sleep(0.2)

# Beat 2 — pivot to Rachel; Mike sneaks a follow-up while she thinks.
send_to("rachel", "Rachel, what do you think about lunch?")
_agent_reply("claude", "By the way, four is also two times two.")  # off-focus → herald
time.sleep(0.15)
_agent_reply("rachel", "Pasta sounds good.")                       # addressee → plays
time.sleep(0.4)

# Beat 3 — user grants Mike permission, his buffer drains in order.
user_says("Sure, Mike, what is it?")
time.sleep(0.3)

# Beat 4 — pivot to Bella; Rachel and Mike both raise their hands.
send_to("bella", "Bella, recommend a song.")
_agent_reply("rachel", "Actually, soup is even better.")           # off-focus → herald
_agent_reply("claude", "I have three quick math facts about pasta.")  # off-focus → herald
_agent_reply("bella",  "Bossa nova vibes.")                        # addressee → plays
time.sleep(0.5)

# Beat 5 — grant ONLY Rachel; Mike stays held.
user_says("Go ahead, Rachel.")
time.sleep(0.3)

# Beat 6 — explicitly address Mike (clears his herald via set_awaiting).
send_to("claude", "Mike, what's up?")
time.sleep(0.4)

print(f"\npending heralds after script: {herald.pending_heralds() or 'none'}")

# Cleanup
import shutil
shutil.rmtree(SCRATCH, ignore_errors=True)

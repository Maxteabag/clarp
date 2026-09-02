"""End-to-end test harness — a fake Claude Code agent.

Wraps the three concrete operations Claude Code performs during a turn so
tests can drive the system through realistic scenarios without launching
the real `claude` binary:

  * UserPromptSubmit hook firing on each new prompt
  * PostToolUse hook firing as in-progress text lands
  * Stop hook firing at end of turn
  * Append assistant text to the JSONL transcript

A `FakeClaude` instance owns:
  - a Claude session UUID (random)
  - an app session name (caller-supplied; registered in the PWA agent DB)
  - a transcript JSONL file path

Use this from a pytest test as:

    with FakeClaude(home=tmp_home, session="rachel",
                    persona="Rachel", voice_id="V") as agent:
        agent.user_prompt("hello there")
        agent.assistant_text("hi from Rachel")
        agent.stop()

After `stop()` the DB has a turns row and a state_log row (done).

Audio is NOT hook-driven any more: the server-side transcript streamer
enqueues into tts_queue and the worker synthesizes. `speak_now()` drives
that seam directly so tests can still assert on clips + sidecars.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK_DIR = REPO / "plugin" / "hooks"
REAL_LIB = REPO / "server" / "lib"


# Subprocess body for `FakeClaude.drain_tts`. Runs in a child Python with
# HOME set to the fake home, so it reads the harness-seeded DB and writes
# audio to the fake home's PWA dir. Prints the number of rows drained on
# stdout so the harness can return it.
_ENQUEUE_SCRIPT = r"""
import os, sys, pathlib
HOME = pathlib.Path(os.environ["HOME"])
sys.path.insert(0, str(HOME / ".local/share/clarp"))
from lib import agents as agents_db
from lib import tts_queue
from lib.protocol import TurnSource
session = os.environ["CLAUDE_PWA_SESSION"]
text = os.environ["CLARP_TEST_SPEAK_TEXT"]
agent = agents_db.get_by_session(session)
assert agent, "no agent registered for " + session
tts_queue.enqueue(
    agent_id=agent["agent_id"],
    text=text,
    voice_id=agent.get("voice_id") or "",
    session=session,
    source=TurnSource.PWA,
    trace_id=agents_db.get_trace(agent["agent_id"]),
    synthesize_audio=True,
)
"""

_DRAIN_SCRIPT = r"""
import os, sys, pathlib
HOME = pathlib.Path(os.environ["HOME"])
sys.path.insert(0, str(HOME / ".local/share/clarp"))
from lib.paths import RuntimePaths
from lib.tts_worker import synth_one
paths = RuntimePaths.from_home(HOME)
paths.audio_dir.mkdir(parents=True, exist_ok=True)
max_iters = int(os.environ.get("CLAUDE_PWA_TTS_DRAIN_MAX", "20"))
n = 0
for _ in range(max_iters):
    if not synth_one(audio_dir=paths.audio_dir):
        break
    n += 1
print(n)
"""


def _seed_schema(db: pathlib.Path) -> None:
    """Create the live schema exactly the way the server and hooks do."""
    db.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-c", "import lib.db as db; db.conn()"],
        cwd=str(REAL_LIB.parent),
        env={**os.environ, "CLAUDE_PWA_DB": str(db)},
        check=True, capture_output=True, text=True, timeout=30,
    )


def _install_fake_eleven(home: pathlib.Path) -> None:
    """Override the test home's lib/eleven_http.py + lib/eleven_ws.py with
    stubs that write a placeholder MP3 instead of calling the real API.
    Both modules need a stub because Phase A routes through eleven_http
    and Phase B routes through eleven_ws — depending on which mode the
    worker picks for a given queue row."""
    target_lib = home / ".local/share/clarp/lib"
    target_lib.parent.mkdir(parents=True, exist_ok=True)
    if not target_lib.exists() and REAL_LIB.is_dir():
        shutil.copytree(REAL_LIB, target_lib)
    (target_lib / "eleven_http.py").write_text(textwrap.dedent("""
        import pathlib
        class ElevenError(Exception): pass
        def synthesize_to_file(text, voice_id, out_path, *, api_key='',
                               model='', speed=1.2, stability=0.5,
                               similarity_boost=0.75, timeout=20.0):
            pathlib.Path(out_path).write_bytes(b'\\xff\\xfb')
            return 2
    """).strip())
    (target_lib / "eleven_ws.py").write_text(textwrap.dedent("""
        import pathlib
        class ElevenWSError(Exception): pass
        def synthesize_streaming(*, text, voice_id, out_path, api_key,
                                 model='eleven_flash_v2_5', speed=1.2,
                                 stability=0.5, similarity_boost=0.75,
                                 timeout=30.0, on_chunk=None, **_kw):
            chunks = [b'\\xff\\xfb', b'\\x90\\x00']
            pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with pathlib.Path(out_path).open('wb') as f:
                for i, c in enumerate(chunks):
                    if on_chunk is not None:
                        try: on_chunk(i, c)
                        except Exception: pass
                    f.write(c); f.flush(); total += len(c)
            return total
    """).strip())


class FakeClaude:
    """One simulated Claude Code agent driving the PWA hook flow."""

    def __init__(self, *, home: pathlib.Path, session: str,
                 persona: str = "Mike", voice_id: str = "v_test"):
        self.home = home
        self.session = session
        self.persona = persona
        self.voice_id = voice_id
        self.backend_session_id = str(secrets.token_hex(8))
        self.transcript = home / "tx" / f"{self.backend_session_id}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()

    def __enter__(self) -> "FakeClaude":
        config = self.home / ".config/clarp/config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            '[tts]\nprovider = "elevenlabs"\nfallback = "none"\n\n'
            '[elevenlabs]\napi_key = "simulated"\n')
        _seed_schema(self.home / ".local/share/clarp/state.sqlite")
        _install_fake_eleven(self.home)
        # Default audio mode = pwa so clips land in the PWA queue.
        # Insert this agent.
        db = self.home / ".local/share/clarp/state.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("""INSERT OR IGNORE INTO agents
            (agent_id, persona, voice_id, cwd, session, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (secrets.token_hex(8), self.persona, self.voice_id,
             str(self.home), self.session, int(time.time() * 1000)))
        con.commit(); con.close()
        return self

    def __exit__(self, *a):
        return False

    # ---- hook drivers --------------------------------------------------

    def _env(self) -> dict:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CLARP_SHARE_DIR"] = str(self.home / ".local/share/clarp")
        env["CLARP_CONFIG_DIR"] = str(self.home / ".config/clarp")
        env["CLARP_CACHE_DIR"] = str(self.home / ".cache/clarp")
        env["CLAUDE_PWA_CONFIG"] = str(
            self.home / ".config/clarp/config.toml")
        env["CLAUDE_PWA_LOG_DIR"] = str(self.home / ".cache/clarp/logs")
        env["CLAUDE_PWA_SESSION"] = self.session
        env.pop("CLAUDE_PWA_DB", None)
        return env

    def _run(self, hook_path: pathlib.Path, *, stdin_payload: dict | None = None,
             argv: list[str] | None = None,
             pwa_voice_marker: bool = False,
             trace_id: str = "") -> subprocess.CompletedProcess:
        if pwa_voice_marker:
            # Drop the marker pwa_source_flag.py looks for. Format mirrors
            # what /transcribe writes: "pwa-voice <session> <ts> [<trace_id>]".
            marker = self.home / ".cache/clarp/source-markers" / self.session
            marker.parent.mkdir(parents=True, exist_ok=True)
            line = f"pwa-voice {self.session} {time.time():.3f}"
            if trace_id:
                line += f" {trace_id}"
            marker.write_text(line)
        cmd = [sys.executable, str(hook_path)] + (argv or [])
        return subprocess.run(
            cmd,
            input=(json.dumps(stdin_payload) if stdin_payload else None),
            env=self._env(),
            capture_output=True, text=True, timeout=15,
        )

    def user_prompt(self, text: str, *, source: str = "pwa",
                    trace_id: str = "") -> None:
        """Fire UserPromptSubmit. `source=pwa` arms the pwa-voice marker so
        the Stop hook routes audio into the PWA queue.

        Also appends a 'user' entry to the transcript — Claude Code writes
        the user message to the JSONL before processing, and the cursor's
        first-run logic uses the most recent user index to define 'current
        turn'."""
        with self.transcript.open("a") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"content": text},
            }) + "\n")
        self._run(
            HOOK_DIR / "pwa_source_flag.py",
            stdin_payload={"session_id": self.backend_session_id,
                           "transcript_path": str(self.transcript)},
            pwa_voice_marker=(source == "pwa"),
            trace_id=trace_id,
        )

    def assistant_text(self, text: str) -> None:
        """Append a chunk of assistant text to the transcript."""
        with self.transcript.open("a") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }) + "\n")

    def stop(self) -> subprocess.CompletedProcess:
        """Fire the Stop hook (stop_state.py), which records the DONE state
        for turns the server did not dispatch itself. It produces no audio —
        use speak_now() for that."""
        return self._run(
            HOOK_DIR / "stop_state.py",
            stdin_payload={"session_id": self.backend_session_id,
                           "transcript_path": str(self.transcript)},
        )

    def speak_now(self, text: str) -> int:
        """Enqueue one utterance the way the server-side transcript streamer
        does, then drain the worker. Returns the rows processed."""
        env = self._env()
        env["CLARP_TEST_SPEAK_TEXT"] = text
        r = subprocess.run([sys.executable, "-c", _ENQUEUE_SCRIPT],
                           env=env, capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, r.stderr
        return self.drain_tts()

    def drain_tts(self, *, max_iters: int = 20) -> int:
        """Synchronously run the TTS worker against this home's DB until
        the queue is empty. Returns the number of rows processed.

        Runs in a subprocess with HOME pointed at the fake home so the
        worker reads the right DB + writes to the right audio dir."""
        env = self._env()
        env["CLAUDE_PWA_TTS_DRAIN_MAX"] = str(max_iters)
        # Inline driver: import the lib and tick synth_one until empty.
        r = subprocess.run(
            [sys.executable, "-c", _DRAIN_SCRIPT],
            env=env, capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return 0
        try:
            return int((r.stdout or "0").strip())
        except ValueError:
            return 0

    # ---- DB introspection ---------------------------------------------

    def db_rows(self, table: str) -> list[tuple]:
        con = sqlite3.connect(str(self.home / ".local/share/clarp/state.sqlite"))
        try:
            return list(con.execute(f"SELECT * FROM {table}"))
        finally:
            con.close()

    def clips_on_disk(self) -> list[pathlib.Path]:
        d = self.home / ".cache/clarp/audio"
        return sorted(d.glob("*.mp3")) if d.is_dir() else []

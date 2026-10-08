#!/usr/bin/env python3
"""End-to-end test harness: boots a real claude-pwa server with deterministic
fakes (controllable ElevenLabs stub, fake STT) and exposes a JSON
control protocol over stdin/stdout for a Node-side test driver to drive.

Why subprocess + stdio: the Node test wants to spawn this, run the FULL
server (real ThreadingHTTPServer, real worker, real clip-id stream broker,
real herald) on a random port, send a few control commands ("synth this
text"), then tear it down. stdin/stdout gives a clean process-isolation
boundary and avoids weird shared-state surprises between Python tests and
the Node test runner.

Protocol (line-delimited JSON):
  → from Node:  {"cmd":"ready"}
  ← to Node:    {"port": 7700, "audio_dir": "/tmp/...", "agent_id": "..."}

  → from Node:  {"cmd":"synth","text":"hello","voice":"V_MIKE","session":"claude",
                "chunks":["AAA","BBB",...],"chunk_delay_ms":100}
  ← to Node:    {"queue_id": 1, "filename": "1779800000000__claude.mp3"}

  → from Node:  {"cmd":"exit"}
  ← to Node:    {"ok": true}

The fake ElevenLabs writes `chunks` to the mp3 file one at a time with
`chunk_delay_ms` between them, then EOS. This lets the test compress the
synthesis-still-in-flight window into a known size, and drives the same
race surface that breaks bugs C-2 / C-3 in production.
"""
from __future__ import annotations

import json
import io
import os
import pathlib
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time

# /tmp is a small RAM-backed tmpfs and this folder can grow large, so it goes
# to /var/tmp unless the caller chose a TMPDIR (the same rule as
# tests/conftest.py's default_temp_root).
def _scratch_root(environ, *, var_tmp_exists: bool) -> str | None:
    if environ.get("TMPDIR") or not var_tmp_exists:
        return None
    return "/var/tmp"


_TMPDIR = pathlib.Path(tempfile.mkdtemp(
    prefix="claude-pwa-e2e-",
    dir=_scratch_root(os.environ, var_tmp_exists=os.path.isdir("/var/tmp"))))


# Cleanup belongs to a separate reaper process, not to this one: vitest's
# afterAll ends us with SIGTERM (or worse), server threads keep writing into
# _TMPDIR until the interpreter is gone, and provider CLIs the server started
# outlive us. The reaper waits until our end of a pipe closes, however we
# died, then kills everything left in our process group and removes _TMPDIR.
def _log(message: str) -> None:
    print(f"server_harness: {message}", file=sys.stderr, flush=True)


def _group_members(pgid: int) -> list[int]:
    members = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # exited while we looked
        state, _ppid, pgrp = stat.rsplit(")", 1)[1].split()[:3]
        if int(pgrp) == pgid and state != "Z":
            members.append(int(entry.name))
    return members


def _reap(pgid: int | None, scratch: pathlib.Path, wait_fd: int) -> None:
    while os.read(wait_fd, 4096):
        pass
    if pgid is not None and os.path.isdir("/proc"):
        for pid in _group_members(pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while (left := _group_members(pgid)) and time.monotonic() < deadline:
            time.sleep(0.02)
        if left:
            _log(f"processes still running after cleanup: {left}")
    if sys.version_info >= (3, 12):
        shutil.rmtree(scratch, onexc=lambda _f, path, exc: _log(
            f"could not remove {path}: {exc!r}"))
    else:
        shutil.rmtree(scratch, onerror=lambda _f, path, info: _log(
            f"could not remove {path}: {info[1]!r}"))


def _start_reaper() -> int:
    """Fork the reaper; return the fd whose closing tells it we are gone."""
    pgid: int | None = os.getpid()
    try:
        # Lead our own group so the reaper finds what we started without
        # touching the caller's processes.
        os.setpgid(0, 0)
    except OSError as exc:
        _log(f"cannot lead a process group, children will not be reaped: {exc!r}")
        pgid = None
    read_fd, write_fd = os.pipe()
    if os.fork():
        os.close(read_fd)
        return write_fd
    try:
        os.setpgid(0, 0)
        for sig in (signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        os.close(write_fd)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)  # stdout is the protocol pipe; do not hold it open
        os.close(devnull)
        _reap(pgid, _TMPDIR, read_fd)
    except BaseException as exc:  # noqa: BLE001 - a fork must never return
        _log(f"reaper failed: {exc!r}")
        os._exit(1)
    os._exit(0)


_REAPER_FD = _start_reaper()

# The server uses print(...) for HTTP access logging, which would collide
# with our JSON-on-stdout protocol. Redirect Python's stdout to stderr and
# keep the original stdout fd for protocol replies only.
_PROTOCOL_OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", buffering=0),
                              write_through=True)

# Make the server's `lib` package importable.
_HERE = pathlib.Path(__file__).resolve()
_SERVER = _HERE.parents[2] / "server"
sys.path.insert(0, str(_SERVER))

# Isolate DB + HOME BEFORE importing any lib modules — db.py reads
# CLAUDE_PWA_DB at import time. Without this we'd write into the user's
# real ~/.local/share/clarp/state.sqlite and conflict with the
# running production server's data (agent UNIQUE constraint, etc).
os.environ["HOME"] = str(_TMPDIR)
os.environ["XDG_CONFIG_HOME"] = str(_TMPDIR / "config")
os.environ["XDG_DATA_HOME"] = str(_TMPDIR / "data")
os.environ["XDG_CACHE_HOME"] = str(_TMPDIR / "cache")
os.environ["CLAUDE_PWA_DB"] = str(_TMPDIR / "state.sqlite")
os.environ["CLAUDE_PWA_CONFIG"] = str(_TMPDIR / "config/clarp/config.toml")
os.environ["CLAUDE_PWA_PORT"] = "0"  # we pick a free port ourselves
_config = pathlib.Path(os.environ["CLAUDE_PWA_CONFIG"])
_config.parent.mkdir(parents=True, exist_ok=True)
_config.write_text(
    '[tts]\nprovider = "elevenlabs"\nfallback = "none"\n\n'
    '[elevenlabs]\napi_key = "simulated"\n')

from lib import agents as agents_db                # noqa: E402
from lib import provider_capabilities              # noqa: E402
from lib import tts_queue                          # noqa: E402
from lib import tts_worker as _tw                  # noqa: E402
from lib.audio_stream import AudioStream           # noqa: E402
from lib.context import ServerContext, StubSTT     # noqa: E402
from lib.protocol import TurnSource       # noqa: E402
from lib.tts_engine import FakeTTSEngine           # noqa: E402

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER / "server.py")
assert _spec and _spec.loader
_srv_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_srv_mod)
build_server = _srv_mod.build_server

# The cache warm-up probes every provider CLI for the model catalogue. With
# HOME pointing here, the developer's `opencode` and `grok` run through mise,
# which installs whole toolchains into this folder (hundreds of megabytes)
# and rewrites its global config. Report every provider as not installed.
provider_capabilities._resolve_executable = lambda _provider_id: None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- controllable fake ElevenLabs ---------------------------------------

# Per-clip override of the bytes written + chunk pacing. The synth() command
# stashes a config here keyed by app session, then the next claim from
# tts_queue picks it up.
_NEXT_SYNTH: dict[str, dict] = {}


def _fake_synthesize_streaming(*, text, voice_id, out_path, on_chunk=None, **_kw):
    """Drop-in for lib.eleven_ws.synthesize_streaming.

    Honors out_path=None (the HlsDelivery path: bytes flow exclusively via
    on_chunk, no mp3 on disk). When out_path is set (ChunkedFileDelivery)
    we still write the mp3 — same observable shape as production."""
    if out_path is not None:
        out = pathlib.Path(out_path)
        session = out.stem.split("__", 1)[-1]
    else:
        out = None
        # Without a path to parse, use the last-synth session key stashed by
        # Harness.synth so we can still pick up _NEXT_SYNTH config.
        session = _LAST_SESSION[0] if _LAST_SESSION else "claude"
    cfg = _NEXT_SYNTH.pop(session, None) or {}
    chunks = cfg.get("chunks") or [b"\xff\xfb\x90\x00", b"AAAA", b"BBBB"]
    chunks = [c.encode() if isinstance(c, str) else c for c in chunks]
    delay = float(cfg.get("chunk_delay_ms", 0)) / 1000.0
    total = 0
    f = None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        f = out.open("wb")
    try:
        for i, c in enumerate(chunks):
            if on_chunk is not None:
                on_chunk(i, c)
            if f is not None:
                f.write(c); f.flush()
            total += len(c)
            if delay:
                time.sleep(delay)
    finally:
        if f is not None:
            f.close()
    return total


_LAST_SESSION: list[str] = []     # see Harness.synth — used when out_path is None


# ---- harness state -------------------------------------------------------


class Harness:
    def __init__(self):
        # _TMPDIR was created at module top-level so we could redirect
        # HOME + CLAUDE_PWA_DB before any `lib` import happened.
        self.tmpdir = _TMPDIR
        # Use the SAME path the TTSWorker writes to so the HTTP routes
        # (which read from ctx.audio_dir) can find the artifacts. Production
        # has these aligned via ServerContext.production(); the harness has
        # to do it explicitly because we build ctx manually.
        from lib.paths import RuntimePaths
        _paths = RuntimePaths.from_home(self.tmpdir)
        self.audio_dir = _paths.audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.agents_path = self.tmpdir / "agents.json"

        # Patch the worker's import of synthesize_streaming so PWA-mode
        # rows hit our fake instead of opening a real WS to ElevenLabs.
        _tw.synthesize_streaming = _fake_synthesize_streaming

        self.agent_id = agents_db.create_agent(
            persona="Mike", voice_id="V_MIKE",
            cwd=str(self.tmpdir), session="claude",
        )
        agents_db.set_focus(self.agent_id)

        static_dir = _SERVER.parent / "static"
        ctx = ServerContext(
            root=self.tmpdir, static=static_dir, audio_dir=self.audio_dir,
            agents_path=self.agents_path,
            default_session="claude",
            tts=FakeTTSEngine(self.audio_dir),
            stream=AudioStream(self.audio_dir),
            stt=StubSTT(text="hello", ends_terminal=True),
            roster_names=("Mike",),
        )

        # Herald, wired before build_server so the server-owned TTSWorker
        # routes direct clip events through the same arbitration policy as
        # production.
        from lib.herald import HeraldManager
        self.herald = HeraldManager(
            stream=ctx.stream, tts=ctx.tts,
            agents=lambda: agents_db.session_dict(),
        )
        self.herald.set_focus("claude")
        ctx.replace_service("herald", self.herald)

        self.port = _free_port()
        self.srv = build_server(ctx, self.port, bind_addr="127.0.0.1")
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._t.start()
        self._wait_for_listening()

    def _wait_for_listening(self):
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/agents/snapshot", timeout=0.1).read()
                return
            except Exception:
                time.sleep(0.02)
        raise RuntimeError("server did not start")

    def synth(self, *, text, voice, session,
              chunks=None, chunk_delay_ms=0) -> dict:
        if chunks:
            # Decode hex-encoded chunks if the test asked for that (real
            # mp3 bytes don't survive JSON cleanly otherwise). Strings
            # without "hex:" prefix are passed through as utf-8.
            decoded = []
            for c in chunks:
                if isinstance(c, str) and c.startswith("hex:"):
                    decoded.append(bytes.fromhex(c[4:]))
                else:
                    decoded.append(c)
            _NEXT_SYNTH[session] = {
                "chunks": decoded, "chunk_delay_ms": chunk_delay_ms,
            }
        # Stash the last-used session key so _fake_synthesize_streaming can
        # find _NEXT_SYNTH config when out_path=None (HlsDelivery path).
        if _LAST_SESSION:
            _LAST_SESSION[0] = session
        else:
            _LAST_SESSION.append(session)
        trace_id = f"e2e-{int(time.time()*1e6)}"
        queue_id = tts_queue.enqueue(
            agent_id=self.agent_id, text=text, voice_id=voice,
            session=session, source=TurnSource.PWA,
            trace_id=trace_id,
        )
        # Returning trace_id lets the Node test match the SSE 'audio'
        # event to this exact synth, even when prior tests' clips are
        # still being broadcast.
        return {"queue_id": queue_id, "trace_id": trace_id}

    def shutdown(self):
        try: self.srv.shutdown(); self.srv.server_close()
        except Exception: pass


# ---- stdio protocol ------------------------------------------------------


def _reply(obj):
    _PROTOCOL_OUT.write(json.dumps(obj) + "\n")
    _PROTOCOL_OUT.flush()


def main():
    h = Harness()
    # Initial banner so the Node side can read the port without sending a
    # command first.
    _reply({"ready": True, "port": h.port,
            "tmpdir": str(h.tmpdir),
            "audio_dir": str(h.audio_dir),
            "agent_id": h.agent_id})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            _reply({"error": f"bad json: {e}"})
            continue
        try:
            name = cmd.get("cmd")
            if name == "ready":
                _reply({"port": h.port})
            elif name == "synth":
                _reply(h.synth(
                    text=cmd["text"], voice=cmd["voice"], session=cmd["session"],
                    chunks=cmd.get("chunks"),
                    chunk_delay_ms=cmd.get("chunk_delay_ms", 0),
                ))
            elif name == "spawn_straggler":
                # Test-only: a child that ignores SIGTERM and keeps writing
                # into the scratch HOME, as a mise install would.
                import subprocess
                p = subprocess.Popen(
                    ["sh", "-c", 'trap "" TERM; while :; do mkdir -p "$HOME/late"; sleep 0.05; done'],
                    stdin=subprocess.DEVNULL)
                _reply({"pid": p.pid})
            elif name == "exit":
                _reply({"ok": True})
                h.shutdown()
                return
            else:
                _reply({"error": f"unknown cmd: {name}"})
        except Exception as e:
            _reply({"error": str(e), "type": type(e).__name__})


if __name__ == "__main__":
    main()

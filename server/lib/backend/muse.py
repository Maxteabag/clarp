"""Muse Code (``muse exec --json``): Meta's coding CLI, one headless run per turn.

docs/architecture/muse-backend.md is the contract: the wire it reads, how a
session id is minted and continued, and where the transcript lives.
"""
from __future__ import annotations

import json
import hashlib
import re
import signal
import socket
import threading
import time
import os
import pathlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import agents as agents_db
from .. import muse_transcript
from ..tts_queue_records import enqueue as enqueue_tts
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..turn_lifecycle import TurnEvent
from ..voice_preamble import apply_voice_preamble
from .base import BackendBrand
from .stream_json import StreamJsonBackend, iter_json_dicts, stop_after_drain_failure
from .long_prompt import PromptFile, fits_argv, removing

# Terminal states that end a run without an answer. A stop (Clarp's own, or
# the user's) is not a failure: like Codex and Claude it leaves no error row.
_FAILED = {"failed", "aborted"}
_STOPPED = {"cancelled", "interrupted"}


@dataclass
class _TurnState:
    """Accumulators for one turn's drainer thread.

    ``live_text`` is the assistant text since the last tool result (Muse
    commits one assistant message per model call, so a tool call ends the
    segment); ``turn_text`` is every segment of the turn.
    """
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    turn_text: str = ""
    failed_error: str = ""
    stopped: bool = False
    saw_session: bool = False
    phase: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    allowance: dict = field(default_factory=dict)
    seen_speak: set[str] = field(default_factory=set)
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0


def _payload(ev: dict) -> dict:
    payload = ev.get("payload")
    return payload if isinstance(payload, dict) else {}


def _usage_from(payload: dict) -> tuple[int, int]:
    tokens_in = tokens_out = 0
    for row in payload.get("models") or []:
        if not isinstance(row, dict):
            continue
        try:
            tokens_in += int(row.get("input_tokens") or 0)
            tokens_out += int(row.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
    return tokens_in, tokens_out


def _tool_name(payload: dict) -> str:
    facts = payload.get("correlation_facts")
    facts = facts if isinstance(facts, dict) else {}
    return str(facts.get("tool_name") or payload.get("tool_name") or "tool")[:80]


# Serialize Muse spawn/registration with orphan verification. An in-progress
# spawn must never look like a forgotten handle between launch and register.
_RECOVERY_LOCK = threading.RLock()
_RECLAIM_GRACE = 2.0


class _MuseHandle(TurnHandle):
    """One caller-visible turn, including its single lock-recovery attempt."""

    def __init__(self, original: TurnHandle):
        super().__init__(original.proc, original.drain_thread, original.process_group)
        self._replacement_lock = threading.RLock()
        self._stop_requested = False

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        drain = self.drain_thread
        if drain is not None and drain is not threading.current_thread():
            drain.join(timeout=timeout)
            if drain.is_alive():
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        return self.proc.wait(timeout=remaining)

    def is_alive(self):
        return (super().is_alive()
                or self.drain_thread is not None and self.drain_thread.is_alive())

    def terminate(self):
        with self._replacement_lock:
            self._stop_requested = True
            return super().terminate()

    def kill(self):
        with self._replacement_lock:
            self._stop_requested = True
            super().kill()

    def replace(self, spawn):
        with self._replacement_lock, _RECOVERY_LOCK:
            if self._stop_requested:
                return None
            proc, replacement = spawn()
            self.proc, self.process_group = proc, replacement.process_group
            return proc


def _kernel_lock_holder(lock, pid):
    """The file's informational pid is insufficient: require the actual flock."""
    info = lock.stat()
    device = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for line in pathlib.Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        major, minor, inode = fields[5].split(':')
        if (int(major, 16), int(minor, 16), int(inode)) == device:
            return int(fields[4]) == pid
    return False


def _owned_holder(pid, *, agent_id, session, session_id, provider):
    """Fail closed unless this exact child is a Clarp-issued headless turn."""
    from .. import db, provider_background_jobs
    root = pathlib.Path(f'/proc/{pid}')
    stat = root.joinpath('stat').read_text().rsplit(')', 1)[1].split()
    if (root.stat().st_uid != os.getuid() or int(stat[1]) != os.getpid()
            or int(stat[2]) != pid or int(stat[3]) != pid):
        return 'foreign'
    argv = root.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
    # Script stand-ins run under Python; the executable token still has to be
    # the Muse command, followed immediately by exec (never TUI resume).
    if argv and re.fullmatch(r'python(?:[0-9.]+)?', pathlib.Path(argv[0]).name):
        argv = argv[1:]
    options = argv[:argv.index('--')] if '--' in argv else argv
    if (len(options) < 3 or not re.fullmatch(r'muse(?:-bin(?:-[0-9.]+)?)?',
                                           pathlib.Path(options[0]).name)
            or options[1] != 'exec' or '--json' not in options or '--yolo' not in options
            or options.count('--session-id') != 1):
        return 'foreign'
    pos = options.index('--session-id')
    if pos + 1 >= len(options) or options[pos + 1] != session_id:
        return 'foreign'
    env = dict(part.split('=', 1) for part in root.joinpath('environ').read_text().split('\0')
               if '=' in part)
    if not agent_id or not session or env.get('CLAUDE_PWA_SESSION') != session:
        return 'foreign'
    token = env.get(provider_background_jobs.TURN_ENV, '')
    if not provider_background_jobs._TOKEN.fullmatch(token):
        return 'unverifiable'
    row = db.conn().execute('''SELECT agent_id,provider,pid,start_token FROM provider_turns
                              WHERE turn_token=?''', (token,)).fetchone()
    # Match the recorded Linux boot/start identity using this verification's
    # existing stat snapshot; no feature-layer process observer is needed.
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    start = f'{boot}:{stat[19]}'
    if (not row or row['agent_id'] != agent_id or row['provider'] != provider
            or row['pid'] != pid or not start or row['start_token'] != start):
        return 'unverifiable'
    # WNOWAIT verifies this is still our waitable child without freeing the
    # pid. Do not reap until all process-group signals have been sent.
    if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
        return 'unverifiable'
    return ''


class MuseBackend(StreamJsonBackend):
    """Runs ``muse exec --json --session-id <id>`` once per turn."""
    # --- catalogue data ---------------------------------------------------
    id = 'muse'
    label = 'Muse Code'
    required_binary = 'muse'
    aliases = ('muse-code', 'musecode')
    badge = 'BackendMuse'
    detail = 'Runs on Meta Muse Code.'
    symbol = 'sparkles'
    brand = BackendBrand('#1c2b4a', '#0d1526', '#8fb4ff', '#2b4a8a')
    efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
    # Each model lists its own effort tiers in Muse's catalogue cache.
    effort_scope = 'model'
    runner = 'muse'
    config_model_field = 'muse_model'
    config_effort_field = 'muse_effort'
    model_family = 'muse'
    fallback_models = (
        ('muse-wonder-1.0', 'Muse Wonder 1.0'),
    )

    transcript = muse_transcript

    # --- argv ---------------------------------------------------------------

    def build_cmd(self, session_id: str = "", *, model: str = "", effort: str = "") -> list[str]:
        """argv for one headless turn; the prompt follows as ``-- <prompt>``.

        ``--session-id`` both creates a session and continues it, so a new
        agent and a resumed one differ only in whether the id was just
        minted. ``--yolo`` turns approval and the sandbox off for the run,
        the posture every Clarp backend runs with.
        """
        cmd = [self.required_binary, "exec", "--json", "--yolo"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--reasoning-effort", effort]
        if session_id:
            cmd += ["--session-id", session_id]
        return cmd

    # --- the turn -------------------------------------------------------------

    def start_turn(
        self,
        *,
        text: str,
        cwd: pathlib.Path,
        backend_session_id: str = "",
        is_new_session: bool = False,
        session: str = "",
        agent_id: str = "",
        on_session_init: Optional[Callable[[str], None]] = None,
        on_result: Optional[Callable[[dict], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        trace_id: str = "",
        stream: Any = None,
        enqueue: Optional[Callable[..., int]] = None,
        voice_preamble: bool = False,
        model: str = "",
        effort: str = "",
        isolated: bool = False,
        **_kwargs: Any,
    ) -> TurnHandle:
        muse_bin = self.required_binary
        if shutil.which(muse_bin) is None:
            raise FileNotFoundError(
                f"`{muse_bin}` not on PATH — install Muse Code "
                f"(https://api.meta.ai/muse-launcher.sh) to run Muse-backed agents")
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        minted = ""
        if not backend_session_id or is_new_session:
            minted = str(uuid.uuid4())
            backend_session_id = minted
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        prompt = apply_voice_preamble(text, voice=voice_preamble, persona=persona, session=session)
        cmd = self.build_cmd(backend_session_id, model=model, effort=effort)
        prompt_file = None
        flag = "new" if minted else "resume"
        log("museSpawn", f"cwd={cwd} {flag}={backend_session_id} text_len={len(text)} "
                         f"trace={trace_id or '∅'} agent={agent_id or '∅'}")
        runtime_agent_id = "" if isolated else agent_id
        from .. import provider_background_jobs as provider_jobs
        turn_token, identity = provider_jobs.identity_env(runtime_agent_id)
        try:
            if fits_argv(prompt):
                cmd += ["--", prompt]
            else:
                # Past one argv string (BACKENDS-08); removed when the turn ends.
                prompt_file = PromptFile(prompt)
                cmd += ["--prompt-file", prompt_file.path]
            with _RECOVERY_LOCK:
                proc, original = self.launch(cmd, cwd=cwd, session=session, env_extra=identity)
                handle = _MuseHandle(original)
                provider_jobs.record_identity(turn_token, agent_id=runtime_agent_id,
                                              provider=self.id, pid=proc.pid)
                self.register_handle(runtime_agent_id, handle)

            def retry_spawn():
                token, retry_identity = provider_jobs.identity_env(runtime_agent_id)
                with _RECOVERY_LOCK:
                    replacement = self.launch(cmd, cwd=cwd, session=session, env_extra=retry_identity)
                    provider_jobs.record_identity(token, agent_id=runtime_agent_id,
                                                  provider=self.id, pid=replacement[0].pid)
                    return replacement
            self.start_drain(
                handle, removing(self._drain_stdout, prompt_file),
                proc=proc, agent_id=runtime_agent_id, session=session,
                trace_id=trace_id, handle=handle,
                backend_session_id=backend_session_id, minted=bool(minted),
                on_session_init=on_session_init, on_result=on_result,
                on_error=on_error, stream=stream,
                enqueue=enqueue or enqueue_tts, retry_spawn=retry_spawn,
            )
        except BaseException:
            # Until the drain owns it, nothing else removes the prompt file.
            if prompt_file is not None:
                prompt_file.remove()
            raise
        return handle

    def _drain_stdout(
        self, *, proc: subprocess.Popen, agent_id: str, session: str, trace_id: str,
        handle: TurnHandle, backend_session_id: str, minted: bool,
        on_session_init, on_result, on_error, stream, enqueue, retry_spawn=None,
    ) -> None:
        st = _TurnState(session_id=backend_session_id)
        reported = False        # a terminal outcome reached on_result/on_error
        try:
            # The id is Clarp's own, so the agent is bound to it before Muse
            # writes a line: a crash mid-run still resumes the same session.
            self.bind_session(st, backend_session_id, on_session_init=on_session_init,
                              on_error=on_error, trace_id=trace_id)
            if minted:
                self._transition(agent_id, TurnEvent.SPAWN_STARTED,
                                 {"dispatch": self.runner, "trace_id": trace_id})
            for attempt in range(2):
                rc, saw_output = self._read_attempt(
                    proc, st, agent_id=agent_id, session=session, trace_id=trace_id,
                    on_error=on_error, stream=stream, enqueue=enqueue)
                err = stderr_text(proc) if rc else ''
                locked = (rc != 0 and not saw_output and not st.failed_error and not st.stopped
                          and err.strip() == f"session {backend_session_id} is already in use")
                if not locked:
                    break
                detail = 'retry_exhausted'
                if attempt == 0 and retry_spawn is not None:
                    detail = self._reclaim_session(backend_session_id, agent_id=agent_id,
                                                   session=session)
                    if not detail:
                        replacement = handle.replace(retry_spawn)
                        if replacement is not None:
                            proc = replacement
                            continue
                        detail = 'stopped'
                err = ("muse_session_locked: Muse session is held or could not be reclaimed; "
                       f"{detail}. Close the other Muse process and resend.")
                break
            if rc != 0 and not st.failed_error and not st.stopped:
                err = err or f"muse exited rc={rc}"
                log("museExitErr", f"rc={rc} trace={trace_id or '∅'} stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    reported = True
                    on_error(err)
            elif on_result is not None and not st.failed_error:
                self._persist_live_text(st, agent_id=agent_id, session=session,
                                        trace_id=trace_id, stream=stream, force=True)
                self._broadcast(stream, agent_id, session)
                usage = ({"input_tokens": st.tokens_in, "output_tokens": st.tokens_out}
                         if (st.tokens_in or st.tokens_out) else {})
                reported = True
                on_result({"last_agent_message": st.last_agent_message or st.turn_text,
                           "usage": usage})
        except Exception as error:  # noqa: BLE001
            log_exception("museDrainFail", error, detail=trace_id)
            # Nothing reads this CLI any more: stop it, and end the turn once.
            stop_after_drain_failure(proc, handle)
            if on_error is not None and not st.failed_error and not reported:
                try:
                    on_error(str(error)[:300])
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.cancel_live_text(st)
            if agent_id:
                self.unregister_handle(agent_id, handle)

    def register_handle(self, agent_id, handle):
        with _RECOVERY_LOCK:
            super().register_handle(agent_id, handle)

    def _read_attempt(self, proc, st, **callbacks):
        saw_output = False
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                saw_output = True
                if st.failed_error:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    self._handle_event(ev, st, **callbacks)
        return proc.wait(), saw_output

    def _reclaim_session(self, session_id, *, agent_id, session):
        """Return an empty refusal only after a verified owned holder exits."""
        pid, signals, outcome = 0, [], 'unverifiable'
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:12]
        try:
            if not re.fullmatch(r'[0-9a-fA-F-]{8,64}', session_id):
                return outcome
            paths = list(muse_transcript.sessions_root().glob(
                f'[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/{session_id}/.session.lock'))
            if len(paths) != 1:
                return outcome
            lock = paths[0]
            fields = dict(line.split('=', 1) for line in lock.read_text()[:4096].splitlines()
                          if '=' in line)
            pid = int(fields.get('pid', '0'))
            if pid <= 1 or fields.get('host') != socket.gethostname():
                return outcome
            with _RECOVERY_LOCK:
                def verify():
                    reason = _owned_holder(pid, agent_id=agent_id, session=session,
                                           session_id=session_id, provider=self.id)
                    if reason:
                        return reason
                    # A handle under ANY agent protects the holder, even if
                    # its registry key disagrees with the turn's stored owner.
                    if self._registry is None:
                        return 'unverifiable'
                    with self._registry._lock:
                        handles = [h for group in self._registry._active.values() for h in group]
                    if any((h.pid == pid or h.process_group == pid) and h.is_alive()
                           for h in handles):
                        return 'live'
                    if not _kernel_lock_holder(lock, pid):
                        return 'unverifiable'
                    return ''

                outcome = verify()
                if outcome:
                    return f'{outcome} holder pid {pid}'
                # The un-reaped child reserves its pid/group through escalation.
                os.killpg(pid, signal.SIGTERM)
                signals.append('TERM')
                deadline = time.monotonic() + _RECLAIM_GRACE
                while time.monotonic() < deadline:
                    exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if exited is not None:
                        os.waitpid(pid, 0)
                        outcome = 'reclaimed'
                        return ''
                    time.sleep(0.05)
                # No process identity or live ownership assumption is carried
                # across the grace interval. Unknown means no further signal.
                outcome = verify()
                if outcome:
                    return f'{outcome} holder pid {pid}'
                os.killpg(pid, signal.SIGKILL)
                signals.append('KILL')
                deadline = time.monotonic() + _RECLAIM_GRACE
                while time.monotonic() < deadline:
                    if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                        os.waitpid(pid, 0)
                        outcome = 'reclaimed'
                        return ''
                    time.sleep(0.05)
                outcome = 'still_running'
                return f'{outcome} holder pid {pid}'
        except (OSError, ValueError, IndexError, UnicodeError) as error:
            outcome = 'unverifiable'
            log_exception('museReclaimVerificationFail', error, detail=f'pid={pid} session_hash={digest}')
            return f'{outcome} holder pid {pid}'
        except Exception as error:  # noqa: BLE001
            outcome = 'unverifiable'
            log_exception('museReclaimFail', error, detail=f'pid={pid} session_hash={digest}')
            return f'{outcome} holder pid {pid}'
        finally:
            log('museSessionReclaim', f'pid={pid} session_hash={digest} '
                f'signals={",".join(signals) or "none"} outcome={outcome}')

    def _handle_event(
        self, ev: dict, st: _TurnState, *, agent_id: str, session: str, trace_id: str,
        on_error, stream, enqueue,
    ) -> None:
        kind = str(ev.get("payload_type") or "")
        payload = _payload(ev)
        if kind == "run.model.configured":
            st.model = str(payload.get("model_id") or "")
            return
        if kind == "run.lifecycle.started":
            st.phase = "thinking"
            self._transition(agent_id, TurnEvent.TEXT_STREAMED,
                             {"dispatch": self.runner, "trace_id": trace_id, "phase": "thinking"})
            return
        if kind == "run.usage":
            tokens_in, tokens_out = _usage_from(payload)
            if tokens_in or tokens_out:
                st.tokens_in, st.tokens_out = tokens_in, tokens_out
            allowance = payload.get("allowance")
            if isinstance(allowance, dict):
                st.allowance = allowance
            return
        if kind == "tool.result":
            # The assistant message that called this tool is committed to
            # session.jsonl by now: flush the live segment so the durable row
            # takes over, show the tool, and refresh the history pane.
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            st.live_text = ""
            st.persisted_live_text = ""
            st.phase = "tool"
            self._transition(agent_id, TurnEvent.TOOL_STARTED,
                             {"dispatch": self.runner, "trace_id": trace_id,
                              "tool": _tool_name(payload)})
            self._broadcast(stream, agent_id, session)
            return
        if kind.startswith("run.terminal."):
            terminal = str(payload.get("terminal") or kind.rsplit(".", 1)[-1])
            if terminal in _STOPPED:
                st.stopped = True
            elif terminal in _FAILED:
                reason = str(payload.get("reason") or "").strip()
                st.failed_error = (f"Muse Code {terminal}: {reason}" if reason
                                   else f"Muse Code {terminal}")
                if on_error is not None:
                    on_error(st.failed_error)
                return
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            self._broadcast(stream, agent_id, session)
            return
        if kind != "run.output.delta":
            return
        delta = str(payload.get("text") or "")
        if not delta:
            return
        if st.phase != "speaking":
            st.phase = "speaking"
            self._transition(agent_id, TurnEvent.TEXT_STREAMED,
                             {"dispatch": self.runner, "trace_id": trace_id, "phase": "responding"})
        st.live_text += delta
        st.turn_text += delta
        st.last_agent_message = st.live_text
        self.speak(st.live_text, st, agent_id=agent_id, session=session, trace_id=trace_id,
                   enqueue=enqueue, fail_event="museSpeakFail", fail_detail=trace_id)
        self._persist_live_text(st, agent_id=agent_id, session=session,
                                trace_id=trace_id, stream=stream)

    def _persist_live_text(self, st: _TurnState, *, agent_id: str, session: str,
                           trace_id: str, stream: Any, force: bool = False) -> None:
        """One mutable assistant row at a bounded cadence, keyed by the turn's
        trace; the transcript import replaces it once session.jsonl has the
        committed message (same contract as the Codex, AGY and Grok runners)."""
        self.persist_live_text(st, text=st.live_text, backend_session_id=st.session_id,
                               agent_id=agent_id, session=session, trace_id=trace_id,
                               stream=stream, force=force, interval=self.live_text_interval)

    def _transition(self, agent_id: str, turn_event: str, detail: dict[str, Any]) -> None:
        self.transition(agent_id, turn_event, detail, log_event="museStateFail")

    def _broadcast(self, stream: Any, agent_id: str, session: str) -> None:
        self.broadcast_transcript(stream, agent_id, session)

    # --- interactive terminal ----------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """``muse --yolo resume <id>``, or a fresh TUI when there is no session
        yet; approval and the sandbox are off, as for the agent's own turns."""
        argv = [self.required_binary, "--yolo"]
        if session_id:
            argv += ["resume", session_id]
        return argv

    # ---- orchestrator routing ----------------------------------------------

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one headless request with no stored session (orchestrator)."""
        return self.build_cmd(model=model, effort=effort) + ["--no-session-log", "--", prompt]

    def routing_text(self, stdout: str) -> str:
        """The run's answer: its terminal text, else the concatenated deltas."""
        deltas = ""
        for ev in iter_json_dicts(stdout):
            kind = str(ev.get("payload_type") or "")
            payload = _payload(ev)
            if kind == "run.terminal.completed" and payload.get("text"):
                return str(payload["text"])
            if kind == "run.output.delta":
                deltas += str(payload.get("text") or "")
        return deltas

    # No ``compaction``: Muse compacts its own context (``--context-compaction-*``)
    # and a typed ``/compact`` in ``muse resume`` is not verified, so the
    # catalogue says compaction is unsupported.

"""Claude Code: transcript-file resume, hook plugin and the dispatch lock.

The Claude backend can use either the official `claude` binary or the `clarp`
wrapper. Both expose the Claude Code print-mode stream-json contract and still
write the standard JSONL transcript at
~/.claude/projects/<sanitised-cwd>/<uuid>.jsonl, so the existing
transcript_streamer inotify watcher continues to drive TTS + history-pane
updates unchanged.

The runner's only jobs:
  * spawn the configured Claude CLI with the right flags (stream-json + partial
    messages + --resume <id> when we already know the session UUID, --continue
    or nothing on a fresh agent)
  * drain stdout on a background daemon thread and parse the JSON lines
  * surface the few events the rest of the server cares about:
      - system.init  →  carries the session_id; let the caller stamp
                         it onto the agent's live runtime row so
                         transcript_streamer subscribes to the JSONL
      - result       →  terminal event for the turn; lets the caller
                         release any per-turn herald / busy state

The runner is fire-and-forget from the caller's perspective: it returns
the handle, and callbacks fire from the drainer thread. It is not a
stream-json backend in the sense of ``StreamJsonBackend`` (the prompt goes
over a stdin pipe and the PWA side-effects come from the hook plugin), but
it shares the subprocess contract (``launch``, ``start_drain``).

Which executable runs is the Host ``claude_cli`` setting, read by
``executable()`` against the ``executable_choices`` catalogue data.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import shutil
import subprocess
import time
import uuid
from typing import Any, Callable, Optional

from .. import agents as agents_db
from .. import config as _config
from .. import events
from .claude_models import claude_model_pin
from .. import takeover as turn_takeover_steps
from .. import turn_lifecycle
from ..turn_lifecycle import TurnEvent
from ..stream_pacing import LivePacer
from ..message_text import claude_block_is_line
from ..claude_reply_evidence import ClaudeReplyEvidence, REPLY_CODEC
from ..log import log, log_exception, log_fault
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..voice_preamble import persona_identity_instruction
from ..backend_catalogue import CLAUDE
from .base import CatalogueDefault, BackendBrand, CliMissing, Backend, CompactionStrategy, Unsupported, resolve
from .stream_json import launch, start_drain, stop_after_drain_failure


# The namespace of the uuids the Host writes on Claude's stdin lines.
_PROMPT_UUID_NAMESPACE = uuid.UUID("5f0e7c52-9a5b-4d9e-8f53-2f0b9b1d4910")


def prompt_uuid(trace_id: str) -> str:
    """The uuid the Host writes on the stdin line of the turn of ``trace_id``:
    the same for the same trace, so it can be named again later (a takeover
    drops that entry, issue 491). A turn without a trace gets a fresh one."""
    trace = (trace_id or "").strip()
    if not trace:
        return str(uuid.uuid4())
    return str(uuid.uuid5(_PROMPT_UUID_NAMESPACE, trace))


def note_output(handle, ev: dict) -> None:
    """What a takeover judges (issue 491, part B): every line the drain has
    read, and whether any of them was output: a word or a tool call, in a
    stream event or a whole assistant message. Thinking alone is nothing."""
    handle.lines_read = int(getattr(handle, "lines_read", 0)) + 1
    if getattr(handle, "output_seen", False):
        return
    kinds = []
    if ev.get("type") == "stream_event":
        inner = ev.get("event") or {}
        block = inner.get("content_block") or {}
        delta = inner.get("delta") or {}
        kinds = [block.get("type"), delta.get("type")]
    elif ev.get("type") == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        kinds = [part.get("type") for part in content if isinstance(part, dict)]
    if any(kind in {"text", "text_delta", "tool_use", "input_json_delta", "server_tool_use"}
           for kind in kinds):
        handle.output_seen = True


# Steering a running Claude turn (issue 491, part C; recordings 2, 9, 10, 11): a follow-up
# is written to the turn's stdin and is read by the CLI when the running tool call returns,
# inside the same turn; one answer covers both. The CLI's replay echo (--replay-user-messages)
# of that line says when it was read: that is the moment its row is due. Written only while a
# tool call is open; a turn that only writes would answer it as a second turn of its own.
STEER_MIN_VERSION = (2, 1, 321)


class _Follow:
    __slots__ = ("on_read", "on_unread", "results_at_write")

    def __init__(self, on_read, on_unread, results_at_write: int):
        self.on_read, self.on_unread = on_read, on_unread
        self.results_at_write = results_at_write


class SteerState:
    """A Claude turn's open stdin and what its drain has seen of the stream."""

    def __init__(self, stdin):
        import threading
        self.lock = threading.Lock()
        self.stdin = stdin
        self.open = True
        self.tools: set[str] = set()      # tool calls started, no result yet
        self.results = 0                  # top-level results printed
        self.pending: dict[str, _Follow] = {}
        # Lines waiting to be written, in order, by one writer thread that holds no lock
        # while it writes: a follow-up bigger than the pipe, written while the CLI does not
        # read its stdin, must never hold the lock the drain needs for every line.
        self.outbox: list[tuple[str, str]] = []
        self.writing = False

    def close(self) -> None:
        """No more follow-ups: EOF to the CLI (the caller holds the lock). With a write
        under way, the writer closes stdin after its last line."""
        if self.open:
            self.open = False
            if not self.writing:
                self._close_stdin()

    def _close_stdin(self) -> None:
        try:
            self.stdin.close()
        except (OSError, ValueError):
            pass

    def send(self, uid: str, line: str) -> None:
        """Queue one line for the writer (the caller holds the lock)."""
        self.outbox.append((uid, line))
        if not self.writing:
            import threading
            self.writing = True
            threading.Thread(target=self._write_all, name="claude-steer-writer",
                             daemon=True).start()

    def _write_all(self) -> None:
        while True:
            with self.lock:
                if not self.outbox:
                    self.writing = False
                    if not self.open:
                        self._close_stdin()
                    return
                uid, line = self.outbox.pop(0)
            try:
                self.stdin.write(line)
                self.stdin.flush()
                continue
            except (OSError, ValueError) as exc:
                log_exception("claudeSteerWriteFail", exc, detail=uid)
            with self.lock:                       # not delivered: it runs as its own turn
                follow = self.pending.pop(uid, None)
            if follow is not None:
                try:
                    follow.on_unread()
                except Exception as exc:  # noqa: BLE001
                    log_exception("claudeSteerUnreadFail", exc, detail=uid)


def _tool_ids(ev: dict) -> tuple[list[str], list[str]]:
    """(tool calls started, tool calls answered) in one stream line."""
    started, answered = [], []
    if ev.get("type") == "stream_event":
        block = (ev.get("event") or {}).get("content_block") or {}
        if block.get("type") in ("tool_use", "server_tool_use") and block.get("id"):
            started.append(str(block["id"]))
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if ev.get("type") == "assistant" and part.get("type") in ("tool_use", "server_tool_use") \
                    and part.get("id"):
                started.append(str(part["id"]))
            if ev.get("type") == "user" and part.get("type") == "tool_result" \
                    and part.get("tool_use_id"):
                answered.append(str(part["tool_use_id"]))
    return started, answered


def note_steer(handle, ev: dict) -> bool:
    """The drain's side of steering. Returns True for a replay echo (not a stream event
    the rest of the drain should see)."""
    state = getattr(handle, "steer_state", None)
    if state is None:
        return bool(ev.get("type") == "user" and ev.get("isReplay"))
    if ev.get("type") == "user" and ev.get("isReplay"):
        uid = str(ev.get("uuid") or "")
        with state.lock:
            follow = state.pending.pop(uid, None)
            joined = follow is not None and state.results == follow.results_at_write
        if follow is not None:
            try:
                follow.on_read(joined)
            except Exception as exc:  # noqa: BLE001 - the stream goes on
                log_exception("claudeSteerReadFail", exc, detail=uid)
        return True
    started, answered = _tool_ids(ev)
    with state.lock:
        state.tools.update(started)
        state.tools.difference_update(answered)
        if ev.get("type") == "result":
            state.results += 1
            state.tools.clear()
            if not state.pending:
                state.close()
    return False


def end_steering(handle) -> None:
    """The process is done: no more follow-ups; any written but never read stays its own
    message (it runs as its own turn)."""
    state = getattr(handle, "steer_state", None)
    if state is None:
        return
    with state.lock:
        state.close()
        unread = list(state.pending.values())
        state.pending.clear()
    for follow in unread:
        try:
            follow.on_unread()
        except Exception as exc:  # noqa: BLE001
            log_exception("claudeSteerUnreadFail", exc)


def _projects_root(home: pathlib.Path | None) -> pathlib.Path | None:
    return (home / ".claude" / "projects") if home is not None else None


def _agent_mcp_servers(session: str, cfg) -> list[str]:
    """Which MCP servers THIS agent should load: the per-agent selection made
    in the app, or none until the agent has been configured."""
    if session:
        agent = agents_db.get_by_session(session)
        if agent is not None:
            from ..mcp_selection import decode
            configured, servers = decode(agent.get("mcp_servers"))
            if configured:
                return servers
    return []


def _scoped_mcp_config_path(session: str, server_names: list[str]) -> Optional[str]:
    """Write a scoped MCP config containing ONLY the named servers (looked up in
    the user's global ~/.claude.json catalog) and return its path. Returns None
    if none of the names resolve to a known server."""
    catalog = _config.read_global_mcp_servers()
    chosen = {name: catalog[name] for name in server_names if name in catalog}
    if not chosen:
        return None
    from .. import xdg
    out_dir = xdg.config_dir() / "mcp"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{session or 'default'}.json"
        path.write_text(json.dumps({"mcpServers": chosen}))
    except OSError as e:
        log_exception("scopedMcpWriteFail", e, detail=session)
        return None
    return str(path)


def _is_assistant_event(ev: dict) -> bool:
    typ = ev.get("type")
    if typ == "assistant":
        return True
    if typ == "stream_event":
        inner = ev.get("event")
        return isinstance(inner, dict) and str(inner.get("type") or "").startswith(
            ("message_", "content_block_")
        )
    msg = ev.get("message")
    return isinstance(msg, dict) and msg.get("role") == "assistant"


def _assistant_event_text(ev: dict) -> tuple[str, bool]:
    """Best-effort text extraction for Claude stream-json assistant events.

    Claude versions and wrappers have used both cumulative message snapshots
    and delta-like chunks. Return (text, is_delta); the caller reconciles the
    stream into one mutable live row.
    """
    delta = ev.get("delta")
    if isinstance(delta, dict):
        text = _content_text(delta.get("text") or delta.get("content"))
        if text:
            return text, True
    if isinstance(delta, str):
        return delta, True
    inner = ev.get("event")
    if isinstance(inner, dict):
        inner_type = str(inner.get("type") or "")
        if inner_type == "content_block_delta":
            inner_delta = inner.get("delta")
            if isinstance(inner_delta, dict):
                text = _content_text(
                    inner_delta.get("text") or inner_delta.get("content")
                )
                if text:
                    return text, True
        if inner_type == "content_block_start":
            text = _content_text(inner.get("content_block"))
            if text:
                return text, False
    for key in ("text", "content"):
        text = _content_text(ev.get(key))
        if text:
            return text, False
    msg = ev.get("message")
    if isinstance(msg, dict):
        text = _content_text(msg.get("content") or msg.get("text"))
        if text:
            return text, False
    return "", False


def _stream_phase(ev: dict) -> str:
    """thinking / responding / tool for a partial-message event, else ""."""
    if ev.get("type") != "stream_event":
        return ""
    inner = ev.get("event")
    if not isinstance(inner, dict):
        return ""
    inner_type = inner.get("type")
    if inner_type == "content_block_start":
        block = inner.get("content_block")
        block_type = block.get("type") if isinstance(block, dict) else ""
    elif inner_type == "content_block_delta":
        delta = inner.get("delta")
        block_type = delta.get("type") if isinstance(delta, dict) else ""
    else:
        return ""
    if block_type in {"thinking", "redacted_thinking", "thinking_delta", "signature_delta"}:
        return "thinking"
    if block_type in {"text", "text_delta"}:
        return "responding"
    if block_type in {"tool_use", "server_tool_use", "input_json_delta"}:
        return "tool"
    return ""


def _report_phase(agent_id: str, trace_id: str, phase: str, dispatch: str) -> None:
    try:
        turn_lifecycle.transition(agent_id, TurnEvent.TEXT_STREAMED,
                                  {"dispatch": dispatch, "trace_id": trace_id, "phase": phase})
    except Exception as e:                            # noqa: BLE001
        log_exception("clarpPhaseFail", e, detail=trace_id or agent_id)


def _live_items(agent_id: str, conversation, trace_id: str, *, isolated: bool = False):
    """The live items of one Claude turn. Each text block's item names the row its block is
    stored under (R108): block 0's is the live row the stream writes; the later blocks' are the
    ids the import gives them. ``conversation`` is read when an item is made: the turn may bind
    its conversation after the drain began."""
    from ..live_claude import ClaudeLiveItems
    from ..message_import_common import _identity_id
    from ..message_live import _live_message_id
    return ClaudeLiveItems(
        agent_id="" if isolated else agent_id, trace_id=trace_id,
        row_id=lambda key: _live_message_id(agent_id, conversation(), trace_id, key),
        block_row_id=lambda key, block: _identity_id(agent_id, conversation(), trace_id, key, block))


def _starts_text_block(ev: dict) -> bool:
    inner = ev.get("event") if ev.get("type") == "stream_event" else None
    if not isinstance(inner, dict) or inner.get("type") != "content_block_start":
        return False
    block = inner.get("content_block")
    return isinstance(block, dict) and block.get("type") == "text"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        if content.get("type"):
            # A block that is not text (a tool use, a server tool's result, thinking) adds no
            # words: never a Python rendering of its structure into a chat row (R143).
            return ""
        return _content_text(content.get("text") or content.get("content") or "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


@functools.lru_cache(maxsize=8)
def _thinking_display_support(executable: str, stamp: float) -> bool:
    """Whether this Claude CLI takes ``--thinking-display`` (2.1.2xx+).

    Claude 5.x and Opus 4.7+ omit thinking text unless asked for a summary.
    The option is not in ``--help``; a CLI that knows it rejects a bad value
    with its allowed choices, an older one ignores it under ``--version``.
    Cached per binary and modification time.
    """
    try:
        probe = subprocess.run(
            [executable, "--thinking-display", "nonsense", "--version"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    output = (probe.stdout or "") + (probe.stderr or "")
    return probe.returncode != 0 and "summarized" in output


def _wants_summarized_thinking(executable: str) -> bool:
    path = shutil.which(executable) or executable
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return False
    return _thinking_display_support(path, stamp)


def _event_message_id(ev: dict) -> str:
    """The Anthropic message id an assistant event belongs to, if it says."""
    inner = ev.get("event")
    if isinstance(inner, dict) and inner.get("type") == "message_start":
        msg = inner.get("message")
        return str(msg.get("id") or "") if isinstance(msg, dict) else ""
    if ev.get("type") == "assistant":
        msg = ev.get("message")
        return str(msg.get("id") or "") if isinstance(msg, dict) else ""
    return ""


def _is_message_stop(ev: dict) -> bool:
    inner = ev.get("event")
    return isinstance(inner, dict) and inner.get("type") == "message_stop"


def _settle_live_message(*, agent_id: str, backend_session_id: str, trace_id: str,
                         item_key: str, session: str, stream, block: int = 0) -> None:
    if not agent_id or not backend_session_id:
        return
    try:
        if agents_db.mark_live_message_final(
                agent_id=agent_id, backend_session_id=backend_session_id,
                trace_id=trace_id, item_key=item_key,
                **({"block": block} if block else {})) and stream is not None:
            events.broadcast(stream, events.transcript_updated(
                agent_id=agent_id, session=session,
                backend_session_id=backend_session_id))
    except Exception as e:                            # noqa: BLE001
        log_exception("clarpLiveSettleFail", e, detail=trace_id or agent_id)


def _store_live_partial(*, agent_id: str, backend_session_id: str, trace_id: str,
                        text: str, session: str, stream, item_key: str = "",
                        provider_genuine: bool = False, block: int = 0) -> None:
    if not agent_id or not backend_session_id:
        return
    try:
        row = agents_db.upsert_live_assistant_message(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            trace_id=trace_id,
            text=text,
            item_key=item_key,
            **({"block": block} if block else {}),
            **({"_provider_genuine": True} if provider_genuine else {}),
        )
        if not row or not row.get("changed"):
            return
        if stream is not None:
            events.broadcast(stream, events.transcript_updated(
                agent_id=agent_id, session=session,
                backend_session_id=backend_session_id))
    except Exception as e:                            # noqa: BLE001
        log_exception("clarpLivePartialFail", e, detail=trace_id or agent_id)


# The host's spawn keywords ``start_turn`` takes; the rest (``voice_preamble``,
# ``run_if_owned``, ``synthesize_audio``) belong to the stream runners.
_SPAWN_KWARGS = frozenset({
    "text", "cwd", "backend_session_id", "is_new_session", "session",
    "agent_id", "on_session_init", "on_result", "on_error", "trace_id",
    "model", "effort", "stream", "isolated", "hook_session", "takeover",
})


class ClaudeBackend(Backend):
    """Runs the configured Claude CLI once per turn; the only CLI that resumes
    by transcript file, pre-mints its session id and reports through the hook
    plugin."""
    # --- catalogue data ---------------------------------------------------
    _catalogue_definition = CLAUDE
    id = CatalogueDefault('id')
    label = CatalogueDefault('label')
    required_binary = CatalogueDefault('required_binary')
    badge = CatalogueDefault('badge')
    detail = CatalogueDefault('detail')
    symbol = CatalogueDefault('symbol')
    brand = CatalogueDefault('brand')
    supports_fork = CatalogueDefault('supports_fork')
    supports_transcript_streaming = CatalogueDefault('supports_transcript_streaming')
    supports_transcript_tail_apply = True
    # Eligibility only: source incarnation and consumed-prefix hash still decide.
    supports_frozen_transcript_prefix = True
    # A turn opens at its prompt; the user records the harness injects into a
    # turn are dropped by the parser and open none.
    transcript_turn_opens_at_its_prompt = True
    supports_mcp = CatalogueDefault('supports_mcp')
    supports_usage = CatalogueDefault('supports_usage')
    login_kind = CatalogueDefault('login_kind')
    efforts = CatalogueDefault('efforts')
    effort_scope = CatalogueDefault('effort_scope')
    runner = CatalogueDefault('runner')
    config_model_field = CatalogueDefault('config_model_field')
    config_effort_field = CatalogueDefault('config_effort_field')
    config_account_switch_field = CatalogueDefault('config_account_switch_field')
    context_window = CatalogueDefault('context_window')
    # Host ``claude_cli`` values -> the executable each selects; the first
    # entry is the default an unknown value falls back to.
    executable_choices = (
        ('claude', ('claude', 'claude-code', 'claude_code', 'official')),
        ('clarp', ('clarp', 'clarp-cli', 'clarp_cli')),
    )
    fallback_models = CatalogueDefault('fallback_models')


    # Resume before message 1 and drop it, both messages in one line (recording cases 5, 7).
    takeover_strategy = "resume_pair"

    def takeover_ready(self) -> bool:
        from .. import takeover
        return takeover.claude_version_ok(self.executable(_config.load()))

    def takeover_plan(self, *, session_id: str, home: pathlib.Path | None, cwd: str,
                      running_trace: str, first_text: str) -> dict | None:
        from .. import takeover
        drops_turn = prompt_uuid(running_trace)
        path = self.find_transcript(session_id, home, cwd=cwd)
        return takeover.takeover_plan(takeover.session_facts(path, drops_turn),
                                      drops_turn, first_text)

    # A follow-up is read later than it is written (the replay echo says when).
    steer_reads_later = True

    def steer_ready(self) -> bool:
        """The installed Claude Code is the recorded one or later (part C's recordings)."""
        from .. import takeover
        version = takeover._cli_version(self.executable(_config.load()))
        return version is not None and version >= STEER_MIN_VERSION

    def steer_was_read(self, *, session_id: str, home: pathlib.Path | None, cwd: str,
                       trace_id: str) -> bool | None:
        """Whether the CLI read a follow-up written into its stdin: the session file holds a
        user entry with the uuid its line carried (prompt_uuid). None: the file cannot be
        found or read."""
        if not session_id:
            return None
        path = self.find_transcript(session_id, home, cwd=cwd)
        if path is None:
            return None
        uid = prompt_uuid(trace_id)
        try:
            with open(path, encoding="utf-8", errors="replace") as lines:
                for line in lines:
                    if uid not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue          # a half line a killed write left
                    if isinstance(entry, dict) and entry.get("type") == "user" \
                            and str(entry.get("uuid") or "") == uid:
                        return True
        except OSError:
            return None
        return False

    def steer(self, agent_id: str, text: str, *, client_msg_id: str = "",
              synthesize_audio: bool = False, trace_id: str = "",
              on_read=None, on_unread=None) -> bool:
        """Write a follow-up into the agent's running turn while one of its tool calls is
        open (issue 491, part C). True: written; ``on_read(joined)`` runs when the CLI echoes
        it (``joined``: inside the running turn), ``on_unread()`` when the process ends first.
        False: nothing was written (no turn, no open tool call, stdin closed)."""
        if on_read is None or on_unread is None:
            # Without a way to tell when the CLI read it, a follow-up cannot be
            # placed: Claude is not steerable that way (the Backend contract).
            raise Unsupported(f"{self.id} is steered only with read callbacks")
        uid = prompt_uuid(trace_id)
        line = json.dumps({"type": "user", "uuid": uid,
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": text}]}})
        for handle in self.active_handles(agent_id):
            state = getattr(handle, "steer_state", None)
            if state is None:
                continue
            with state.lock:
                if not state.open or not state.tools:
                    return False
                state.pending[uid] = _Follow(on_read, on_unread, state.results)
                state.send(uid, line + "\n")          # written outside the lock
            return True
        return False

    # --- the runner -------------------------------------------------------

    def executable(self, cfg: _config.Config | None = None) -> str:
        """The Host ``claude_cli`` setting: the official CLI or the clarp wrapper.

        Invalid values fail open to the official CLI and are logged so a typo
        does not strand voice turns on a non-existent runner.
        """
        cfg = cfg or _config.load()
        default = self.executable_choices[0][0]
        raw = (getattr(cfg, "claude_cli", "") or default).strip().lower()
        for binary, spellings in self.executable_choices:
            if raw in spellings:
                return binary
        log("claudeCliProviderInvalid", f"value={raw!r} fallback={default}")
        return default

    def spawn_turn(self, **spec: Any):
        """``start_turn`` with the keywords the ``-p`` runner accepts."""
        kwargs = {k: v for k, v in spec.items() if k in _SPAWN_KWARGS}
        return self.start_turn(**kwargs)

    def build_cmd(self, backend_session_id: str = "", *,
                  is_new_session: bool = False, model: str = "",
                  effort: str = "", persona: str = "", session: str = "") -> list[str]:
        """The argv list used to launch a single turn.

        Three modes:
          * known existing session   →  --resume <id>
          * fresh agent, we picked the uuid →  --session-id <id>
          * no id at all (legacy)    →  no continuity flag

        --session-id lets us assign the conversation's UUID ourselves
        before claude even starts. The /send handler picks a uuid, stamps
        the agent's runtime row, then spawns the Claude CLI with that uuid — by the
        time the UserPromptSubmit hook fires inside claude, the DB already
        knows which agent owns this session, so get_by_backend_session()
        resolves cleanly. This eliminates the race we hit before, where
        the runner's on_session_init callback didn't fire until *after*
        the hook had already given up on identifying the agent.

        --continue is intentionally absent: in shared-cwd setups (e.g.
        Rachel + Bella both in the user's home directory) it resolves to the same
        most-recent JSONL for every fresh agent, causing all of them to
        collide on a single backend_session_id.
        """
        cfg = _config.load()
        cmd = [
            self.executable(cfg), "-p",
            "--dangerously-skip-permissions",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",   # required for stream-json to actually stream
        ]
        if _wants_summarized_thinking(cmd[0]):
            # Thinking shows as a live, collapsible summary (docs/live-items.md).
            cmd += ["--thinking-display", "summarized"]
        # Clarp's agent-state hooks ship as a Claude Code plugin loaded from disk.
        # Passing it here rather than registering in ~/.claude/settings.json keeps
        # the install out of the user's Claude Code configuration entirely.
        from ..deployment import plugin_dir
        _plugin = plugin_dir()
        if _plugin is not None:
            cmd += ["--plugin-dir", str(_plugin)]
        # MCP scoping (per agent). By default a PWA turn ignores the global MCP
        # config (--strict-mcp-config) so a heavy/flaky server like teams-local
        # (which cold-starts a Chromium↔Teams bridge) can't block this turn's
        # startup on its initialize handshake — the failure that left messages
        # undelivered for the full idle timeout. Agents that genuinely need the
        # Teams/Outlook tools opt in by session and get a scoped config file.
        if cfg.mcp_strict:
            cmd.append("--strict-mcp-config")
            servers = _agent_mcp_servers(session, cfg)
            if servers:
                scoped = _scoped_mcp_config_path(session, servers)
                if scoped:
                    cmd += ["--mcp-config", scoped]
        # Optional per-agent model / reasoning-effort pins. Empty → CLI default.
        model = claude_model_pin(model)
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        identity = persona_identity_instruction(persona, session)
        if identity:
            cmd += ["--append-system-prompt", identity]
        if backend_session_id:
            flag = "--session-id" if is_new_session else "--resume"
            cmd += [flag, backend_session_id]
        return cmd

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
        on_result:       Optional[Callable[[dict], None]] = None,
        on_error:        Optional[Callable[[str], None]] = None,
        trace_id: str = "",
        model: str = "",
        effort: str = "",
        stream=None,
        isolated: bool = False,
        hook_session: str | None = None,
        takeover: dict | None = None,
    ) -> TurnHandle:
        """Spawn the configured Claude CLI for ONE turn carrying `text` as the prompt.

        ``takeover`` (issue 491, part B): this turn takes over one that was
        ended before it printed anything. ``{"first": text, "resume_at": U,
        "drops_turn": M}``: resume the session at entry U, dropping turn M
        (message 1, never answered), and give BOTH messages as one user line
        of two text blocks, so one answer covers both. With no ``resume_at``
        (the session's first turn), message 1 is already the session's last,
        unanswered entry: a plain resume with message 2 alone (recording case
        7, first-plain).

        The subprocess is started immediately and a daemon thread is started
        that drains its stdout, parsing stream-json events and calling the
        supplied callbacks. Returns instantly — the caller does not block.

        Raises FileNotFoundError if the configured Claude CLI isn't on PATH (so the
        /send handler can surface a clear 500 to the client).

        `session` is the agent identifier — passed through as an env
        var (CLAUDE_PWA_SESSION) so the UserPromptSubmit hook inside Claude Code
        can identify which agent owns this dispatch BEFORE
        bind_backend_session has stamped the runtime row. This sidesteps the race
        where the hook fires before the runner has captured the session_id
        from the CLI's stream-json stdout.
        """
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        cmd = self.build_cmd(
            backend_session_id,
            is_new_session=is_new_session,
            model=model,
            effort=effort,
            persona=persona,
            session=session,
        )
        # Steering keeps stdin open and needs the CLI's echo of what it read.
        steering = bool(agent_id) and not isolated and self.steer_ready()
        if steering:
            cmd += ["--replay-user-messages"]
        blocks = [text]
        if takeover and takeover.get("resume_at"):
            cmd += ["--resume-session-at", str(takeover["resume_at"]),
                    "--resume-drops-turn", str(takeover["drops_turn"])]
            blocks = [str(takeover.get("first") or ""), text]
        cli_bin = cmd[0]
        if shutil.which(cli_bin) is None:
            hint = (
                "install Claude Code (`claude install stable` or npm package)"
                if cli_bin == self.required_binary
                else "install with `npm i -g clarp-cli` or rebuild from ~/GIT/clarp"
            )
            raise CliMissing(f"`{cli_bin}` not on PATH — {hint}")
        # `--input-format stream-json` makes the CLI read the turn's user message
        # from stdin as a JSON line; a positional prompt is ignored in this mode.
        # (clarp used to accept the positional form, hence the historical bug:
        # passing the prompt as argv + stdin=DEVNULL made clarp exit rc=0 with no
        # init/result — the "clarp exited without stream-json result" symptom.)
        # Every line carries a uuid, which Claude Code keeps as that entry's uuid
        # in its session file. It is derived from the turn's trace, so the Host
        # can name this prompt again without storing it: a takeover drops it
        # with --resume-drops-turn (issue 491, part B).
        user_msg = json.dumps({
            "type": "user",
            "uuid": prompt_uuid(trace_id),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": block} for block in blocks]},
        })
        env_session = session if hook_session is None else hook_session
        flag = "new" if is_new_session else ("resume" if backend_session_id else "∅")
        log("clarpSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                         f"text_len={len(text)} trace={trace_id or '∅'} "
                         f"session={session or '∅'}")
        # stdin is a pipe here (not DEVNULL like the other runners): the prompt
        # travels as a stream-json user message, written right below.
        from .. import provider_background_jobs as provider_jobs
        turn_token = provider_jobs.new_turn_token()
        proc, handle = launch(cmd, cwd=cwd, session=env_session,
                              stdin=subprocess.PIPE,
                              env_extra={provider_jobs.TURN_ENV: turn_token})
        recorded = False
        if agent_id and not isolated:
            try:
                provider_jobs.turn_started(turn_token, agent_id=agent_id,
                                           provider=self.id, pid=proc.pid, trace_id=trace_id)
                recorded = True
            except Exception as e:                        # noqa: BLE001
                log_exception("providerTurnStartFail", e, detail=trace_id)
        if not recorded:
            turn_token = ""  # nothing to close at exit
        # Hand the CLI the user message. Without steering stdin is closed at once, so
        # the CLI sees EOF and runs exactly one turn; with steering (issue 491, part
        # C) it stays open for follow-ups and is closed at the turn's result. Guard the
        # write: if clarp died on spawn the pipe is already broken, and the drainer
        # will surface the non-zero exit.
        try:
            if proc.stdin is not None:
                proc.stdin.write(user_msg + "\n")
                proc.stdin.flush()
                if steering:
                    handle.steer_state = SteerState(proc.stdin)
                else:
                    proc.stdin.close()
        except (BrokenPipeError, OSError) as e:
            log_exception("clarpStdinWriteFail", e, detail=trace_id)
        # A takeover may judge this turn (issue 491, part B): the drain below
        # tells it what the CLI has printed, the process group lets it freeze.
        handle.takeover_capable = not isolated and handle.process_group is not None
        handle.takeover_of = bool(takeover)
        self.register_handle("" if isolated else agent_id, handle)
        start_drain(
            handle, self._drain_stdout, backend=self.runner,
            proc=proc, on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, trace_id=trace_id, agent_id=agent_id,
            handle=handle, session=session, backend_session_id=backend_session_id,
            stream=stream, isolated=isolated, turn_token=turn_token,
        )
        return handle

    def _drain_stdout(
        self,
        proc: subprocess.Popen,
        on_session_init: Optional[Callable[[str], None]],
        on_result:       Optional[Callable[[dict], None]],
        on_error:        Optional[Callable[[str], None]],
        trace_id: str,
        agent_id: str = "",
        handle: Optional[TurnHandle] = None,
        session: str = "",
        backend_session_id: str = "",
        stream=None,
        isolated: bool = False,
        turn_token: str = "",
    ) -> None:
        """Background thread body. Reads stream-json line by line and fires
        the cared-about callbacks. Swallows every exception — drainer errors
        must never propagate into a server thread.

        No turn timer: a turn runs until the subprocess completes or exits. We used
        to SIGTERM turns that went silent past a deadline, but that false-killed
        slow-but-working turns (a big resumed context can take well over a minute to
        its first token, and extended thinking emits nothing in the meantime), and
        those kills triggered a re-deliver loop that left the user waiting minutes.
        With preempt-kill, a new message always interrupts a turn on demand, so the
        timer isn't needed for recovery. A turn that genuinely hangs holds its slot
        until the user preempts it or the process dies — which closes stdout and
        ends the read loop below."""
        saw_init = False
        saw_result = False
        final_result: Optional[dict] = None
        saw_usage_limit = False
        live_text = ""
        phase = ""
        bound = {"backend_session_id": backend_session_id}
        # One live row per provider message (message.id), so a turn of
        # several messages never shows them run together; for a keyed message,
        # one row per text block (R108: ``block``, the line the transcript will
        # hold it as, under the id the import gives that line). Each row grows
        # at markdown-stable boundaries at most every 250 ms, with a trailing
        # write; the final text is written at once and the row is marked final
        # in place.
        message = {"key": "", "text": "", "block": 0, "lines": 0, "open": False, "streamed": False,
                   "started": set()}

        evidence = ClaudeReplyEvidence()

        def new_pacer(key: str, block: int = 0):
            provenance = {"genuine": False}
            paced = LivePacer(lambda text: _store_live_partial(
                agent_id=agent_id, backend_session_id=bound["backend_session_id"],
                trace_id=trace_id, text=text, session=session, stream=stream,
                item_key=key, provider_genuine=provenance["genuine"], block=block))
            return paced, provenance

        pacer, provenance = new_pacer("")
        items = _live_items(agent_id, lambda: bound["backend_session_id"] or "", trace_id,
                            isolated=isolated)

        def settle_message() -> None:
            self._drain_settle(message, pacer, bound, agent_id, trace_id, session, stream)
        isolated_texts: list[str] = []
        live_backend_session_id = backend_session_id
        reported = False        # a terminal outcome reached on_result/on_error
        try:
            if proc.stdout is None:
                return
            for line in turn_takeover_steps.read_lines(proc.stdout, handle):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    # clarp prints occasional banner lines on stderr; if any
                    # leak onto stdout, just skip them.
                    continue
                typ = ev.get("type")
                sub = ev.get("subtype")
                if handle is not None:
                    if note_steer(handle, ev):
                        continue                  # a replay echo is no stream event
                    note_output(handle, ev)
                if evidence.suppress_event(ev):
                    continue
                items.on_event(ev)
                if typ == "rate_limit_event":
                    info = ev.get("rate_limit_info")
                    if (isinstance(info, dict)
                            and info.get("status") in {"rejected", "blocked"}
                            and not saw_usage_limit):
                        # Keep the provider's actual limiting window. A generic
                        # usage message alone cannot distinguish model quota,
                        # the rolling session window, and disabled overage.
                        detail = {"trace": trace_id, "agent": agent_id}
                        for key in ("status", "rateLimitType", "utilization", "resetsAt",
                                    "overageStatus", "overageDisabledReason", "isUsingOverage"):
                            value = info.get(key)
                            if isinstance(value, (str, int, float, bool)):
                                detail[key] = value[:80] if isinstance(value, str) else value
                        log("claudeRateLimitRejected", json.dumps(detail, sort_keys=True))
                        saw_usage_limit = True
                        if on_error is not None:
                            on_error("Claude usage limit reached")
                elif typ == "system" and sub == "init":
                    sid = (ev.get("session_id") or "").strip()
                    if sid:
                        saw_init = True
                        live_backend_session_id = sid
                        bound["backend_session_id"] = sid
                        if not self._drain_init(
                                sid, trace_id, on_session_init, on_error, proc, handle):
                            return
                elif _is_assistant_event(ev):
                    current = _stream_phase(ev)
                    if current and current != phase and agent_id and not isolated:
                        phase = current
                        if current != "tool":   # tools are reported by the hooks
                            _report_phase(agent_id, trace_id, current, self.id)
                    message_id = "" if isolated else _event_message_id(ev)
                    if message_id and message_id != message["key"]:
                        settle_message()
                        message.update(key=message_id, text="", block=0, lines=0,
                                       open=False, streamed=False, started=set())
                        pacer, provenance = new_pacer(message_id)
                    if not isolated and _is_message_stop(ev):
                        settle_message()
                    provenance["genuine"] |= evidence.current_is_genuine
                    if message["key"] and not isolated:
                        live_text = self._drain_text(
                            ev, isolated, isolated_texts, live_text, message, pacer, offer=False)
                        pacer, provenance = self._drain_block(
                            ev, message, pacer, provenance, new_pacer,
                            lambda paced: self._drain_settle(
                                message, paced, bound, agent_id, trace_id, session, stream),
                            evidence)
                    else:
                        live_text = self._drain_text(
                            ev, isolated, isolated_texts, live_text, message, pacer)
                elif typ == "result":
                    # One process can run several CLI turns: resuming a
                    # session whose last process left background tasks
                    # behind answers the queued <task-notification> first,
                    # with its own result, and only then runs this prompt.
                    # The turn ends when the process does, with the last
                    # result; the first one ending it let the next message
                    # start a second Claude on the same session.
                    saw_result = True
                    if not isolated:
                        settle_message()
                    final_result = self._drain_result(ev, isolated, isolated_texts, live_text, evidence)
                    live_text = ""
                    isolated_texts = []
            if handle is not None:
                end_steering(handle)
            rc = proc.wait()
            if final_result is not None and on_result is not None:
                reported = True
                try:
                    on_result(final_result)
                except Exception as e:                    # noqa: BLE001
                    log_exception("clarpRunnerResultCbFail", e,
                                  detail=trace_id)
            error = self._drain_exit_error(proc, rc, saw_usage_limit, saw_result, trace_id)
            if error is not None and on_error is not None:
                reported = True
                try:
                    on_error(error)
                except Exception as e:                    # noqa: BLE001
                    log_exception("clarpRunnerErrCbFail", e, detail=trace_id)
            if not saw_init:
                log("clarpNoInitEvent",
                    f"trace={trace_id or '∅'} — turn ran but stream-json "
                    f"never delivered system.init; transcript_streamer may "
                    f"have missed the session binding")
        except Exception as e:                            # noqa: BLE001
            self._drain_failure(e, proc, handle, on_error, trace_id, reported)
        finally:
            if handle is not None:
                end_steering(handle)              # idempotent: also on an early return
            self._drain_cleanup(
                proc, handle, pacer, message, isolated, saw_result,
                agent_id, turn_token, trace_id)

    def _drain_settle(self, message, pacer, bound, agent_id, trace_id, session, stream):
        """Settle the current message (a keyed message: its open text block) with the pacer
        selected by the drain."""
        if message["text"]:
            pacer.offer(message["text"], final=True)
            _settle_live_message(
                agent_id=agent_id, backend_session_id=bound["backend_session_id"],
                trace_id=trace_id, item_key=message["key"], session=session,
                stream=stream, block=message.get("block", 0))
        pacer.cancel()
        if message.get("open"):
            # A text block with words is a line of the transcript (R108).
            message["lines"] += claude_block_is_line({"type": "text", "text": message["text"]})
            message["open"], message["text"] = False, ""

    @staticmethod
    def _drain_block(ev, message, pacer, provenance, new_pacer, settle, evidence):
        """One event of a keyed message, one live row per text block (R108).

        A text block's row holds only that block's words, under the id its line will have in
        /log (block k counts the blocks the transcript keeps as lines: text with words, a
        client tool use). Thinking, a server tool and its result add nothing to any row. With
        partial messages the blocks come as content_block events; without them each assistant
        event is one block. Returns the pacer and provenance now current."""
        def open_block(text):
            nonlocal pacer, provenance
            pacer, provenance = new_pacer(message["key"], message["lines"])
            provenance["genuine"] |= evidence.current_is_genuine
            message.update(block=message["lines"], open=True, text=text)
            if text:
                pacer.offer(text)

        inner = ev.get("event") if ev.get("type") == "stream_event" else None
        if isinstance(inner, dict):
            message["streamed"] = True
            kind = inner.get("type")
            if kind == "content_block_start":
                message["started"].add(inner.get("index"))
                if message["open"]:
                    settle(pacer)
                block = inner.get("content_block")
                block_type = block.get("type") if isinstance(block, dict) else ""
                if block_type == "text":
                    open_block(str(block.get("text") or ""))
                elif claude_block_is_line(block):
                    message["lines"] += 1
            elif kind == "content_block_delta":
                delta = inner.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta" and delta.get("text"):
                    if not message["open"] and inner.get("index") in message["started"]:
                        return pacer, provenance  # a late delta of a block already settled
                    if not message["open"]:
                        open_block("")          # a text delta whose block start was not seen
                    message["text"] += str(delta["text"])
                    pacer.offer(message["text"])
            elif kind == "content_block_stop" and message["open"]:
                settle(pacer)
        elif ev.get("type") == "assistant" and not message["streamed"]:
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            for part in content if isinstance(content, list) else []:
                part_type = part.get("type") if isinstance(part, dict) else ""
                if part_type == "text" and str(part.get("text") or ""):
                    open_block(str(part["text"]))
                    settle(pacer)
                elif claude_block_is_line(part):
                    message["lines"] += 1
        return pacer, provenance

    def _drain_result(self, ev, isolated, isolated_texts, live_text, evidence):
        """Attach only this result's accumulated assistant text."""
        assistant_text = (
            "\n\n".join(t for t in isolated_texts if t.strip())
            if isolated else live_text
        )
        final_result = (
            {**ev, "_assistant_text": assistant_text}
            if assistant_text else ev)
        if (not assistant_text and not ev.get("last_agent_message")
                and not ev.get("message")
                and evidence.suppresses_result_text(ev.get("result"))):
            # Preserve raw provider diagnostics but prevent the
            # text extractor from falling back to this SDK text.
            final_result = {**ev, "_assistant_text": None,
                            "_clarp_suppressed_reply": True}
        facts = evidence.finish_result()
        if facts is not None:
            final_result = {**final_result, "_clarp_reply_provider": REPLY_CODEC,
                            "_clarp_reply_evidence": facts}
        return final_result

    def _drain_init(self, sid, trace_id, on_session_init, on_error, proc, handle):
        """Report a session binding; False means the callback rejected it."""
        log("clarpSessionInit", f"sid={sid} trace={trace_id or '∅'}")
        if on_session_init is not None:
            try:
                accepted = on_session_init(sid)
                if accepted is False:
                    if proc.poll() is None:
                        handle.terminate() if handle else proc.terminate()
                    if on_error is not None:
                        on_error("backend session binding rejected")
                    return False
            except Exception as e:        # noqa: BLE001
                log_exception("clarpRunnerInitCbFail", e,
                              detail=trace_id or sid)
        return True

    def _drain_text(self, ev, isolated, isolated_texts, live_text, message, pacer, offer=True):
        """Accumulate a provider text event and offer its current message to the pacer (not
        with ``offer`` False: a keyed message's rows are written block by block)."""
        text, is_delta = _assistant_event_text(ev)
        if offer and not isolated and _starts_text_block(ev) and message["text"].strip():
            # A message of several text blocks reads as paragraphs, not run together (R143);
            # the first block stays a prefix of the row, so order A's adoption still matches.
            message["text"] += "\n\n"
        if text:
            if isolated and not is_delta:
                isolated_texts.append(text)
            elif isolated and is_delta:
                if isolated_texts:
                    isolated_texts[-1] += text
                else:
                    isolated_texts.append(text)
            if is_delta:
                live_text += text
            elif text.startswith(live_text):
                live_text = text
            elif live_text and not live_text.endswith(text):
                live_text += text
            else:
                live_text = text
            if not offer:
                return live_text
            current_text = message["text"]
            if is_delta:
                current_text += text
            elif text.startswith(current_text):
                current_text = text
            elif current_text and current_text.endswith(text):
                pass    # a block's snapshot at its stop: its deltas are already in the row
            elif current_text:
                # Another block's whole text (assistant events, one a block): a paragraph.
                current_text += text if current_text.endswith("\n\n") else "\n\n" + text
            else:
                current_text = text
            message["text"] = current_text
            if not isolated:
                pacer.offer(current_text)
        return live_text

    def _drain_exit_error(self, proc, rc, saw_usage_limit, saw_result, trace_id):
        """Read and log the terminal error, before the caller invokes on_error."""
        err = stderr_text(proc)
        if rc != 0 and not saw_usage_limit:
            log("clarpExitErr", f"rc={rc} trace={trace_id or '∅'} "
                                f"stderr={(err or '')[:500]!r}")
            return err or f"clarp exited rc={rc}"
        elif not saw_result and not saw_usage_limit:
            msg = err or (
                f"clarp exited rc={rc} without stream-json result or system.init"
            )
            log("clarpNoResult", f"trace={trace_id or '∅'} "
                                  f"stderr={(err or '')[:500]!r}")
            return msg
        return None

    def _drain_failure(self, e, proc, handle, on_error, trace_id, reported):
        """Stop a failed drain and report only if no terminal callback was reached."""
        log_exception("clarpRunnerDrainFail", e, detail=trace_id)
        # Nothing reads this CLI any more: stop it and end the turn once.
        stop_after_drain_failure(proc, handle)
        if not reported and on_error is not None:
            try:
                on_error(f"clarp drain failed: {str(e)[:300]}")
            except Exception as cb:                   # noqa: BLE001
                log_exception("clarpRunnerErrCbFail", cb, detail=trace_id)

    def _drain_cleanup(self, proc, handle, pacer, message, isolated, saw_result,
                       agent_id, turn_token, trace_id):
        """Settle pacing and process ownership when the drain leaves its try block."""
        if not isolated and not saw_result and message["text"]:
            pacer.offer(message["text"], final=True)
        pacer.cancel()
        if agent_id and handle is not None and not isolated:
            self.unregister_handle(agent_id, handle)
        # This process is gone, and its background Bash tasks with it.
        # (stdout can close a moment before the exit; past that, the
        # observer reaps the turn by its process identity.)
        if turn_token:
            try:
                proc.wait(timeout=5)
            except Exception as exc:                  # noqa: BLE001
                log_fault("claudeExitWaitFailed", exc)
        if turn_token and proc.poll() is not None:
            try:
                from .. import provider_background_jobs as provider_jobs
                provider_jobs.turn_exited(turn_token)
            except Exception as e:                    # noqa: BLE001
                log_exception("providerTurnExitFail", e, detail=trace_id)


    # ---- orchestrator routing ----------------------------------------------

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one isolated, non-persisted Claude request (orchestrator).

        Plain text output: the router's JSON is the whole reply.
        """
        cmd = [
            self.executable(), "-p",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        return cmd

    def routing_text(self, stdout: str) -> str:
        """The reply text of a ``routing_cmd`` run."""
        return stdout or ""

    # --- sessions and transcripts -----------------------------------------

    def native_resume_command(self, session_id: str) -> str | None:
        return f"claude --resume {session_id}"

    def resume_target(self, session_id: str, cwd: str,
                      home: pathlib.Path | None = None):
        """The transcript to resume from, or ``None`` for a ghost session.

        ``None`` unbinds the agent from its conversation, so without a ``cwd``
        a miss from the transcript index is confirmed on disk first.
        """
        if not session_id:
            return None
        found = self.find_transcript(session_id, home, cwd=cwd)
        if found is None and not cwd:
            root = _projects_root(home)
            scan = resolve("claude_transcript", "scan_for_jsonl")
            found = scan(session_id, projects_root=root) if root is not None else scan(session_id)
        return found

    def bind_new_session(self, agent_id: str, session: str, *,
                         uuid_factory: Callable[[], str] | None = None) -> str:
        """Pre-mint the ``--session-id`` and bind it to the agent.

        A collision with an id already bound is retried once; any other
        binding failure is the dispatcher's 500.
        """
        from .. import agents as agents_db
        from ..log import log_exception
        mint = uuid_factory or (lambda: str(uuid.uuid4()))
        backend_session_id = mint()
        try:
            agents_db.bind_backend_session(agent_id, backend_session_id)
        except agents_db.SessionAlreadyBound as e:
            log_exception("sendPreStampCollision", e, detail=session)
            backend_session_id = mint()
            agents_db.bind_backend_session(agent_id, backend_session_id)
        return backend_session_id

    def find_transcript(self, session_id: str,
                        home: pathlib.Path | None = None, *,
                        cwd: str = "") -> pathlib.Path | None:
        """One jsonl per session under ``<home>/.claude/projects/<encoded cwd>/``.

        With a ``cwd`` hint the project dir that encodes it is preferred and
        the rest of the root is scanned as a fallback (the CLI lets a session
        cd, so the file lives wherever the first turn ran); without one the
        inotify-backed index answers.
        """
        root = _projects_root(home)
        if cwd:
            from ..resume import find_session_jsonl
            return find_session_jsonl(
                session_id, cwd, root or _projects_root(pathlib.Path.home()))
        find = resolve("claude_transcript", "find_latest_jsonl")
        return find(session_id, projects_root=root) if root is not None else find(session_id)

    def transcript_cwd(self, transcript: Any) -> str:
        """``~/.claude/projects/-foo-bar`` was written by a session in ``/foo/bar``."""
        from ..resume import _cwd_from_project_dir
        return _cwd_from_project_dir(pathlib.Path(transcript).parent)

    def parse_transcript(self, path) -> list[dict]:
        return resolve("claude_transcript", "parse_turns")(path)

    def fork_conversation(self, session_id: str, *, source_cwd: str, cwd: str) -> str:
        """A copy of the session jsonl under a new id, in ``cwd``'s project dir."""
        from ..fork import fork_session
        return fork_session(session_id, source_cwd, _projects_root(pathlib.Path.home()),
                            dest_cwd=cwd)

    def list_sessions(self, cwd: str, *, limit: int = 20,
                      all_projects: bool = False) -> list[dict]:
        from .. import session_catalog
        return session_catalog.list_claude_sessions(
            cwd, all_projects=all_projects, limit=limit)

    # --- interactive terminal ---------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """``claude --resume <id>`` (or fresh) plus the same plugin the ``-p``
        dispatch loads, so an interactive terminal reports state exactly like
        a dispatched turn."""
        argv = [self.required_binary, "--dangerously-skip-permissions"]
        if session_id:
            argv += ["--resume", session_id]
        plugin = resolve("deployment", "plugin_dir")()
        if plugin is not None:
            argv += ["--plugin-dir", str(plugin)]
        return argv

    # --- model policy -----------------------------------------------------

    def recorded_model(self, session_id: str) -> str:
        """The model of the transcript's last turn."""
        if not session_id:
            return ""
        return resolve("session_models", "transcript_model")(self.id, session_id)

    def cli_default_model(self) -> str:
        """``ANTHROPIC_MODEL``, else the ``model`` in the CLI's settings.json."""
        home = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
        data = json.loads((home / "settings.json").read_text())
        return str(os.environ.get("ANTHROPIC_MODEL") or data.get("model") or "")

    # --- compaction -------------------------------------------------------

    def compaction(self, session: str) -> CompactionStrategy:
        """``/compact`` typed into an interactive resume; print mode never
        auto-compacts, and the Host can watch the jsonl settle instead of
        waiting a fixed window."""
        return CompactionStrategy(
            launch=(self.required_binary, "--dangerously-skip-permissions", "--resume"),
            command="/compact", watches_transcript=True)

    # --- credentials and quota --------------------------------------------

    def account_pool(self) -> str:
        """The pool is named after the CLI; turn_dispatch keys its failover
        coordinator by it and reads ``config_account_switch_field``."""
        return self.id

    def quota_identity(self, window: dict) -> Any:
        """Claude reports the same reset with fractional-second jitter, so the
        notification identity rounds it to the minute. Only the identity is
        rounded; provider data and displayed reset times stay exact."""
        identity = window["window_id"]
        if window.get("kind") and window.get("resets_at"):
            import datetime
            try:
                reset = datetime.datetime.fromisoformat(
                    window["resets_at"].replace("Z", "+00:00"))
                if reset.tzinfo is not None:
                    reset_minute = int((reset.timestamp() + 30) // 60)
                    identity = ["claude-reset-minute-v1", window["kind"], reset_minute]
            except (ValueError, TypeError, OverflowError):
                pass
        return identity

    # --- turn plumbing ----------------------------------------------------

    def wrap_turn_callback(self, fn: Callable[..., Any],
                           lock: Any) -> Callable[..., Any]:
        """Runner callbacks arrive on the runner's own threads; serialise them
        under the dispatch lock."""
        def invoke(*args: Any):
            with lock:
                return fn(*args)
        return invoke

    def arm_source_marker(self, session: str, trace_id: str,
                          synthesize_audio: bool, *,
                          home: pathlib.Path | None = None,
                          now: Callable[[], float] | None = None) -> None:
        """Write the single-use pwa-voice marker the UserPromptSubmit hook
        consumes, so the turn is tagged with its real source."""
        from .. import send_service
        from ..log import log_exception
        try:
            path = send_service.source_marker_path(home or pathlib.Path.home(), session)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(send_service.source_marker_text(
                session=session, trace_id=trace_id, now=(now or time.time)(),
                synthesize_audio=synthesize_audio))
        except OSError as e:
            log_exception("sendLastSourceWriteFail", e)

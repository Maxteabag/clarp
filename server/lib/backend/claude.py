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
from .. import provider_capabilities
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..voice_preamble import persona_identity_instruction
from .base import BackendBrand, Backend, CompactionStrategy, resolve
from .stream_json import launch, start_drain


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


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return str(content.get("text") or content.get("content") or "")
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


def _store_live_partial(*, agent_id: str, backend_session_id: str, trace_id: str,
                        text: str, session: str, stream) -> None:
    if not agent_id or not backend_session_id:
        return
    try:
        row = agents_db.upsert_live_assistant_message(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            trace_id=trace_id,
            text=text,
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
    "model", "effort", "stream", "isolated", "hook_session",
})


class ClaudeBackend(Backend):
    """Runs the configured Claude CLI once per turn; the only CLI that resumes
    by transcript file, pre-mints its session id and reports through the hook
    plugin."""
    # --- catalogue data ---------------------------------------------------
    id = 'claude'
    label = 'Claude'
    required_binary = 'claude'
    badge = 'BackendClaude'
    detail = 'Runs on Claude Code.'
    symbol = 'sparkles'
    brand = BackendBrand('#e08b6a', '#c9603d', '#d97757', '#b85433')
    supports_fork = True
    supports_transcript_streaming = True
    supports_mcp = True
    supports_usage = True
    login_kind = 'cli'
    efforts = ('low', 'medium', 'high', 'xhigh', 'max')
    effort_scope = 'model'
    runner = 'clarp'
    config_model_field = 'claude_model'
    config_effort_field = 'claude_effort'
    config_account_switch_field = 'claude_account_switch_command'
    context_window = 1000000
    # Host ``claude_cli`` values -> the executable each selects; the first
    # entry is the default an unknown value falls back to.
    executable_choices = (
        ('claude', ('claude', 'claude-code', 'claude_code', 'official')),
        ('clarp', ('clarp', 'clarp-cli', 'clarp_cli')),
    )
    fallback_models = (
        ('fable', 'Fable'),
        ('opus', 'Opus'),
        ('sonnet', 'Sonnet'),
        ('haiku', 'Haiku'),
        ('claude-fable-5-1', 'Claude Fable 5.1'),
        ('claude-opus-5-5', 'Claude Opus 5.5'),
        ('claude-opus-5', 'Claude Opus 5'),
        ('claude-sonnet-5', 'Claude Sonnet 5'),
        ('claude-haiku-4-5', 'Claude Haiku 4.5'),
        ('claude-opus-4-8', 'Claude Opus 4.8'),
        ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
    )


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
        model = provider_capabilities.claude_model_pin(model)
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
    ) -> TurnHandle:
        """Spawn the configured Claude CLI for ONE turn carrying `text` as the prompt.

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
        cli_bin = cmd[0]
        if shutil.which(cli_bin) is None:
            hint = (
                "install Claude Code (`claude install stable` or npm package)"
                if cli_bin == self.required_binary
                else "install with `npm i -g clarp-cli` or rebuild from ~/GIT/clarp"
            )
            raise FileNotFoundError(f"`{cli_bin}` not on PATH — {hint}")
        # `--input-format stream-json` makes the CLI read the turn's user message
        # from stdin as a JSON line; a positional prompt is ignored in this mode.
        # (clarp used to accept the positional form, hence the historical bug:
        # passing the prompt as argv + stdin=DEVNULL made clarp exit rc=0 with no
        # init/result — the "clarp exited without stream-json result" symptom.)
        user_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        })
        env_session = session if hook_session is None else hook_session
        flag = "new" if is_new_session else ("resume" if backend_session_id else "∅")
        log("clarpSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                         f"text_len={len(text)} trace={trace_id or '∅'} "
                         f"session={session or '∅'}")
        # stdin is a pipe here (not DEVNULL like the other runners): the prompt
        # travels as a stream-json user message, written right below.
        proc, handle = launch(cmd, cwd=cwd, session=env_session,
                              stdin=subprocess.PIPE)
        # Hand the CLI the single user message, then close stdin so it sees EOF and
        # runs exactly one turn. Guard the write: if clarp died on spawn the pipe
        # is already broken, and the drainer will surface the non-zero exit.
        try:
            if proc.stdin is not None:
                proc.stdin.write(user_msg + "\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (BrokenPipeError, OSError) as e:
            log_exception("clarpStdinWriteFail", e, detail=trace_id)
        self.register_handle("" if isolated else agent_id, handle)
        start_drain(
            handle, self._drain_stdout, backend=self.runner,
            proc=proc, on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, trace_id=trace_id, agent_id=agent_id,
            handle=handle, session=session, backend_session_id=backend_session_id,
            stream=stream, isolated=isolated,
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
        saw_usage_limit = False
        live_text = ""
        isolated_texts: list[str] = []
        live_backend_session_id = backend_session_id
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
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
                if typ == "rate_limit_event":
                    info = ev.get("rate_limit_info")
                    if (isinstance(info, dict)
                            and info.get("status") in {"rejected", "blocked"}
                            and not saw_usage_limit):
                        saw_usage_limit = True
                        if on_error is not None:
                            on_error("Claude usage limit reached")
                elif typ == "system" and sub == "init":
                    sid = (ev.get("session_id") or "").strip()
                    if sid:
                        saw_init = True
                        live_backend_session_id = sid
                        log("clarpSessionInit", f"sid={sid} trace={trace_id or '∅'}")
                        if on_session_init is not None:
                            try:
                                accepted = on_session_init(sid)
                                if accepted is False:
                                    if proc.poll() is None:
                                        handle.terminate() if handle else proc.terminate()
                                    if on_error is not None:
                                        on_error("backend session binding rejected")
                                    return
                            except Exception as e:        # noqa: BLE001
                                log_exception("clarpRunnerInitCbFail", e,
                                              detail=trace_id or sid)
                elif _is_assistant_event(ev):
                    text, is_delta = _assistant_event_text(ev)
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
                        if not isolated:
                            _store_live_partial(
                                agent_id=agent_id,
                                backend_session_id=live_backend_session_id,
                                trace_id=trace_id,
                                text=live_text,
                                session=session,
                                stream=stream,
                            )
                elif typ == "result":
                    saw_result = True
                    if on_result is not None:
                        try:
                            assistant_text = (
                                "\n\n".join(t for t in isolated_texts if t.strip())
                                if isolated else live_text
                            )
                            if assistant_text:
                                ev = {**ev, "_assistant_text": assistant_text}
                            on_result(ev)
                        except Exception as e:            # noqa: BLE001
                            log_exception("clarpRunnerResultCbFail", e,
                                          detail=trace_id)
            rc = proc.wait()
            err = stderr_text(proc)
            if rc != 0 and not saw_usage_limit:
                log("clarpExitErr", f"rc={rc} trace={trace_id or '∅'} "
                                    f"stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    try:
                        on_error(err or f"clarp exited rc={rc}")
                    except Exception as e:                # noqa: BLE001
                        log_exception("clarpRunnerErrCbFail", e, detail=trace_id)
            elif not saw_result and not saw_usage_limit:
                msg = err or (
                    f"clarp exited rc={rc} without stream-json result or system.init"
                )
                log("clarpNoResult", f"trace={trace_id or '∅'} "
                                      f"stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    try:
                        on_error(msg)
                    except Exception as e:                # noqa: BLE001
                        log_exception("clarpRunnerErrCbFail", e, detail=trace_id)
            if not saw_init:
                log("clarpNoInitEvent",
                    f"trace={trace_id or '∅'} — turn ran but stream-json "
                    f"never delivered system.init; transcript_streamer may "
                    f"have missed the session binding")
        except Exception as e:                            # noqa: BLE001
            log_exception("clarpRunnerDrainFail", e, detail=trace_id)
        finally:
            if agent_id and handle is not None and not isolated:
                self.unregister_handle(agent_id, handle)

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

    def resume_target(self, session_id: str, cwd: str,
                      home: pathlib.Path | None = None):
        """The transcript to resume from, or ``None`` for a ghost session."""
        if not session_id:
            return None
        return self.find_transcript(session_id, home, cwd=cwd)

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
        find = resolve("transcript_log", "find_latest_jsonl")
        return find(session_id, projects_root=root) if root is not None else find(session_id)

    def transcript_cwd(self, transcript: Any) -> str:
        """``~/.claude/projects/-foo-bar`` was written by a session in ``/foo/bar``."""
        from ..resume import _cwd_from_project_dir
        return _cwd_from_project_dir(pathlib.Path(transcript).parent)

    def parse_transcript(self, path) -> list[dict]:
        return resolve("transcript_log", "parse_turns")(path)

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

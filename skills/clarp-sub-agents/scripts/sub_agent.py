#!/usr/bin/env python3
"""Spawn sub-agents that survive the parent Clarp session being restarted.

    clarp-sub-agent start NAME WORKDIR PROMPT_FILE [--model M] [--backend B] [--title T] [--clarp-agent]
    clarp-sub-agent status [NAME]
    clarp-sub-agent stop NAME

Default mode: each sub-agent runs as its own systemd user unit
(clarp-sub-agent-NAME), outside the parent session's cgroup, as a raw
`claude -p` / `codex exec` process.

--clarp-agent: the sub-agent is a real Clarp helper agent (role helper, parent =
the calling session, cwd = WORKDIR) that the owner can open and steer. It is
sent the prompt as an agent-origin message and reports back the same way. A
small watcher unit keeps the background job alive until the helper reports.

In default mode the unit registers a background job of kind "worker" on the
parent session: a background process, with its log registered and streamed so
the apps can show the live tail. With --clarp-agent the helper itself is the
sub-agent; its watcher's job (kind "sub-agent", detail = helper session) only
mirrors it and is not counted as a second one.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

DIR = pathlib.Path(os.environ.get("CLARP_SUB_AGENT_DIR", "/var/tmp/clarp-sub-agents"))
DEFAULT_MODEL = "claude-opus-5-5"
HEARTBEAT_SEC = 90
PROGRESS_SEC = 20
PROGRESS_MAX = 200
POLL_SEC = 30
CLARP_BACKENDS = ("claude", "codex", "grok", "agy", "opencode", "deepseek")
# A prompt longer than this many bytes is not inlined into the message; the helper is
# told to read the copied prompt file instead (argv and chat rows stay small).
INLINE_PROMPT_MAX = 60_000
# helper_state -> the background job's end: reported and done are success.
JOB_END = {"reported": "finish", "done": "finish", "failed": "fail", "abandoned": "fail"}


def _parent() -> str:
    return os.environ.get("CLARP_SESSION") or os.environ.get("CLAUDE_PWA_SESSION") or ""


def _bg(*args: str) -> str:
    """clarp-agent-bg, best effort: a sub-agent must run even if the Host is down."""
    if not shutil.which("clarp-agent-bg"):
        return ""
    try:
        out = subprocess.run(["clarp-agent-bg", *args], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "CLARP_BACKGROUND_WORKER_PID": str(os.getpid())})
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _command(backend: str, model: str, prompt: str) -> list[str]:
    if backend == "codex":
        return ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                *(["-m", model] if model else []), prompt]
    # stream-json so the log grows while the agent works; render() turns it
    # back into readable text.
    return ["claude", "-p", prompt, "--model", model or DEFAULT_MODEL,
            "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
            "--max-turns", "400"]


def _tool_summary(block: dict) -> str:
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    hint = next((str(args[k]) for k in ("command", "file_path", "path", "pattern",
                                         "description", "url", "query") if args.get(k)), "")
    hint = " ".join(hint.split())
    return f"→ {block.get('name') or 'tool'}" + (f": {hint[:160]}" if hint else "")


def render(backend: str, line: str) -> list[str]:
    """Readable log lines for one line of backend output.

    Claude's stream-json becomes its assistant text and one short line per
    tool call; anything that is not JSON (codex, errors) passes through.
    """
    line = line.rstrip("\n")
    if backend == "codex" or not line.startswith("{"):
        return [line] if line.strip() else []
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return [line]
    if not isinstance(event, dict):
        return []
    if event.get("type") == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        out: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                out.append(str(block["text"]).rstrip())
            elif block.get("type") == "tool_use":
                out.append(_tool_summary(block))
        return out
    if event.get("type") == "result":
        status = "error" if event.get("is_error") else "done"
        return [f"[{status}] {event.get('num_turns', '?')} turns"]
    return []


def _progress_line(lines: list[str]) -> str:
    for text in reversed(lines):
        first = next((part.strip() for part in text.splitlines() if part.strip()), "")
        if first:
            return first[:PROGRESS_MAX]
    return ""


class AdminError(RuntimeError):
    pass


def _admin(*args: str) -> dict:
    """Run clarp-admin and parse its JSON output."""
    exe = shutil.which("clarp-admin")
    if not exe:
        raise AdminError("clarp-admin is not on PATH")
    out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        detail = (out.stderr.strip().splitlines() or [f"exit {out.returncode}"])[-1]
        raise AdminError(f"clarp-admin {args[0]} {args[1] if len(args) > 1 else ''}: {detail}")
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AdminError(f"clarp-admin returned no JSON: {out.stdout[:200]}") from exc


def _helper_message(name: str, helper: str, parent: str, workdir: pathlib.Path,
                    prompt_file: pathlib.Path) -> str:
    prompt = prompt_file.read_text()
    body = prompt if len(prompt.encode()) <= INLINE_PROMPT_MAX else (
        f"Your task is too long to inline. Read it in full from {prompt_file} first.")
    return (
        f"You are the Clarp helper agent `{helper}` ({name}), working for `{parent}` "
        f"in {workdir}. Stay in that directory.\n\n{body}\n\n"
        "When you are finished (or blocked), send your final report to your parent "
        "in one message:\n"
        f"  clarp-admin prompt --to {parent} --from {helper} --text \"<report>\"\n"
        "That message marks you reported. Do not message your parent for routine progress."
    )


def _live_helper(name: str) -> str:
    """The session of an earlier helper with this name that is still live."""
    saved = DIR / f"{name}.session"
    if not saved.exists():
        return ""
    session = saved.read_text().strip()
    try:
        row = _admin("agent", "helper-state", session)
    except AdminError:
        return ""
    return session if row.get("role") == "helper" and not row.get("archived_at") else ""


def start_clarp_agent(a: argparse.Namespace, workdir: pathlib.Path, prompt: pathlib.Path,
                      model: str) -> int:
    """Create (or reuse) a Clarp helper agent, prompt it, and watch it."""
    parent = _parent()
    if not parent:
        sys.exit("--clarp-agent needs CLARP_SESSION (the parent session)")
    DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompt, DIR / f"{a.name}.prompt.md")
    (DIR / f"{a.name}.title").write_text((a.title or f"Helper {a.name}") + "\n")
    (DIR / f"{a.name}.exit").unlink(missing_ok=True)
    try:
        helper = _live_helper(a.name)
        if helper:
            _admin("agent", "helper-state", helper, "running", "--from", parent)
        else:
            create = ["agent", "create", a.name, "--parent", parent, "--role", "helper",
                      "--cwd", str(workdir), "--backend", a.backend]
            if model:
                create += ["--model", model]
            helper = str(_admin(*create).get("session") or "")
            if not helper:
                raise AdminError("agent create returned no session")
        (DIR / f"{a.name}.session").write_text(helper + "\n")
        _admin("prompt", "--to", helper, "--from", parent, "--text",
               _helper_message(a.name, helper, parent, workdir, DIR / f"{a.name}.prompt.md"))
    except AdminError as exc:
        print(f"clarp-sub-agent: {exc}", file=sys.stderr)
        return 1
    unit = f"clarp-sub-agent-{a.name}"
    log = DIR / f"{a.name}.log"
    if not shutil.which("systemd-run"):
        print(f"started helper {helper} (parent={parent}); no systemd, so no background job")
        return 0
    subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
    passthrough = [f"--setenv={k}={os.environ[k]}" for k in
                   ("PATH", "HOME", "CLARP_SHARE_DIR", "CLARP_CONFIG_DIR", "CLARP_CACHE_DIR",
                    "CLAUDE_PWA_CONFIG", "CLAUDE_PWA_DB", "CLARP_SUB_AGENT_DIR") if os.environ.get(k)]
    subprocess.run(["systemd-run", "--user", "--unit", unit, "--collect",
                    "-p", f"StandardOutput=file:{log}", "-p", f"StandardError=append:{log}",
                    *passthrough, sys.executable, str(pathlib.Path(__file__).resolve()),
                    "__watch", a.name, helper, parent], check=True)
    print(f"started helper {helper} (parent={parent}); open it in Clarp to steer it; "
          f"watcher {unit}; log: {log}")
    return 0


def watch(name: str, helper: str, parent: str, *, poll_sec: float = POLL_SEC,
          sleep=time.sleep) -> int:
    """Body of the watcher unit: hold the sub-agent job until the helper reports."""
    title = (DIR / f"{name}.title").read_text().strip()
    handle = _bg(parent, "job-upsert", f"sub-agent-{name}", "sub-agent", title, helper)
    if handle:
        _bg(parent, "job-active", handle)
    state, misses = "", 0
    while True:
        try:
            state = str(_admin("agent", "helper-state", helper).get("helper_state") or "")
            misses = 0
        except AdminError as exc:
            # The Host restarting is normal; a helper that stays gone is not.
            misses += 1
            if misses >= 20:
                state = "abandoned"
                print(f"helper {helper} unreachable: {exc}", file=sys.stderr)
        if state in JOB_END:
            break
        if handle:
            _bg(parent, "job-heartbeat", handle)
        sleep(poll_sec)
    ok = JOB_END[state] == "finish"
    if handle:
        if ok:
            _bg(parent, "job-finish", handle)
        else:
            _bg(parent, "job-fail", handle, f"helper {helper} {state}")
    (DIR / f"{name}.exit").write_text(f"{0 if ok else 1}\n")
    return 0 if ok else 1


def run(name: str, backend: str, model: str, parent: str) -> int:
    """Body of the systemd unit: register the job, heartbeat it, run, finish it.

    This is a background process, not a sub-agent: kind "worker". Its stdout
    is the log file, registered on the job so the apps can show the tail, and
    the backend's output is rendered into it line by line as it arrives.
    """
    title = (DIR / f"{name}.title").read_text().strip()
    handle = _bg(parent, "job-upsert", f"sub-agent-{name}", "worker", title, name) if parent else ""
    stop = threading.Event()
    latest = {"line": "", "sent": ""}
    if handle:
        _bg(parent, "job-active", handle)
        _bg(parent, "job-log", handle, str((DIR / f"{name}.log").resolve()))

        def beat() -> None:
            waited = 0
            while not stop.wait(PROGRESS_SEC):
                waited += PROGRESS_SEC
                line = latest["line"]
                if line and line != latest["sent"]:
                    latest["sent"] = line
                    _bg(parent, "job-progress", handle, line)
                if waited >= HEARTBEAT_SEC:
                    waited = 0
                    _bg(parent, "job-heartbeat", handle)
        threading.Thread(target=beat, daemon=True).start()
    prompt = (DIR / f"{name}.prompt.md").read_text()
    proc = subprocess.Popen(_command(backend, model, prompt), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, text=True, bufsize=1, errors="replace")
    assert proc.stdout is not None
    for raw in proc.stdout:
        lines = render(backend, raw)
        if lines:
            print("\n".join(lines), flush=True)
            latest["line"] = _progress_line(lines) or latest["line"]
    rc = proc.wait()
    stop.set()
    if handle:
        if rc == 0:
            _bg(parent, "job-finish", handle)
        else:
            _bg(parent, "job-fail", handle, f"sub-agent exited {rc}")
    (DIR / f"{name}.exit").write_text(f"{rc}\n")
    return rc


def start(a: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a.name):
        sys.exit("name must be [A-Za-z0-9._-]")
    workdir, prompt = pathlib.Path(a.workdir).expanduser(), pathlib.Path(a.prompt_file).expanduser()
    if not workdir.is_dir():
        sys.exit(f"no such workdir: {workdir}")
    if not prompt.is_file() or not prompt.read_text().strip():
        sys.exit(f"empty prompt file: {prompt}")
    if a.clarp_agent:
        model = a.model if a.model is not None else ("" if a.backend != "claude" else DEFAULT_MODEL)
        return start_clarp_agent(a, workdir.resolve(), prompt, model)
    if a.backend not in ("claude", "codex"):
        sys.exit("without --clarp-agent, --backend must be claude or codex")
    if not shutil.which("systemd-run"):
        print("needs systemd (Linux); on macOS use launchctl submit", file=sys.stderr)
        return 3
    model = a.model if a.model is not None else ("" if a.backend == "codex" else DEFAULT_MODEL)
    DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompt, DIR / f"{a.name}.prompt.md")
    (DIR / f"{a.name}.title").write_text((a.title or f"Sub-agent {a.name}") + "\n")
    (DIR / f"{a.name}.exit").unlink(missing_ok=True)
    (DIR / f"{a.name}.session").unlink(missing_ok=True)
    unit = f"clarp-sub-agent-{a.name}"
    subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
    log = DIR / f"{a.name}.log"
    passthrough = [f"--setenv={k}={os.environ[k]}" for k in
                   ("PATH", "HOME", "CLARP_SHARE_DIR", "CLARP_CONFIG_DIR", "CLARP_CACHE_DIR",
                    "CLAUDE_PWA_CONFIG", "CLAUDE_PWA_DB") if os.environ.get(k)]
    subprocess.run(["systemd-run", "--user", "--unit", unit, "--collect",
                    "-p", f"WorkingDirectory={workdir}",
                    "-p", f"StandardOutput=file:{log}", "-p", f"StandardError=append:{log}",
                    *passthrough, sys.executable, str(pathlib.Path(__file__).resolve()),
                    "__run", a.name, a.backend, model, _parent()], check=True)
    print(f"started {unit} (parent={_parent() or 'none'}); log: {log}")
    return 0


def status(a: argparse.Namespace) -> int:
    rows = sorted(DIR.glob("*.prompt.md")) if DIR.is_dir() else []
    shown = 0
    for f in rows:
        n = f.name[: -len(".prompt.md")]
        if a.name and n != a.name:
            continue
        state = subprocess.run(["systemctl", "--user", "is-active", f"clarp-sub-agent-{n}"],
                               capture_output=True, text=True).stdout.strip()
        exit_file = DIR / f"{n}.exit"
        log = DIR / f"{n}.log"
        session = DIR / f"{n}.session"
        helper = f" helper={session.read_text().strip()}" if session.exists() else ""
        print(f"{n:<28} {state:<9} exit={exit_file.read_text().strip() if exit_file.exists() else '-':<4} "
              f"log={log.stat().st_size if log.exists() else 0}B{helper}")
        shown += 1
    if not shown:
        print("no sub-agents")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["__run"]:
        return run(*argv[1:5])
    if argv[:1] == ["__watch"]:
        return watch(*argv[1:4])
    p = argparse.ArgumentParser(prog="clarp-sub-agent", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("name"); s.add_argument("workdir"); s.add_argument("prompt_file")
    s.add_argument("--model"); s.add_argument("--backend", choices=CLARP_BACKENDS, default="claude")
    s.add_argument("--title")
    s.add_argument("--clarp-agent", action="store_true",
                   help="run as a Clarp helper agent the owner can open and steer")
    st = sub.add_parser("status"); st.add_argument("name", nargs="?")
    sp = sub.add_parser("stop"); sp.add_argument("name")
    a = p.parse_args(argv)
    if a.cmd == "start":
        return start(a)
    if a.cmd == "status":
        return status(a)
    return subprocess.run(["systemctl", "--user", "stop", f"clarp-sub-agent-{a.name}"]).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

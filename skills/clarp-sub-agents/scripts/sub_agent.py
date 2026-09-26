#!/usr/bin/env python3
"""Spawn sub-agents that survive the parent Clarp session being restarted.

    clarp-sub-agent start NAME WORKDIR PROMPT_FILE [--model M] [--backend claude|codex] [--title T]
    clarp-sub-agent status [NAME]
    clarp-sub-agent stop NAME

Each sub-agent runs as its own systemd user unit (clarp-sub-agent-NAME), outside
the parent session's cgroup, and is registered as a background job of kind
"sub-agent" on the parent session so the phone and desktop show it running.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading

DIR = pathlib.Path(os.environ.get("CLARP_SUB_AGENT_DIR", "/var/tmp/clarp-sub-agents"))
DEFAULT_MODEL = "claude-opus-5-5"
HEARTBEAT_SEC = 90


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
    return ["claude", "-p", prompt, "--model", model or DEFAULT_MODEL,
            "--dangerously-skip-permissions", "--output-format", "text", "--max-turns", "400"]


def run(name: str, backend: str, model: str, parent: str) -> int:
    """Body of the systemd unit: register the job, heartbeat it, run, finish it."""
    title = (DIR / f"{name}.title").read_text().strip()
    handle = _bg(parent, "job-upsert", f"sub-agent-{name}", "sub-agent", title, name) if parent else ""
    stop = threading.Event()
    if handle:
        _bg(parent, "job-active", handle)
        def beat() -> None:
            while not stop.wait(HEARTBEAT_SEC):
                _bg(parent, "job-heartbeat", handle)
        threading.Thread(target=beat, daemon=True).start()
    prompt = (DIR / f"{name}.prompt.md").read_text()
    rc = subprocess.run(_command(backend, model, prompt), stdin=subprocess.DEVNULL).returncode
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
    if not shutil.which("systemd-run"):
        print("needs systemd (Linux); on macOS use launchctl submit", file=sys.stderr)
        return 3
    model = a.model if a.model is not None else ("" if a.backend == "codex" else DEFAULT_MODEL)
    DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompt, DIR / f"{a.name}.prompt.md")
    (DIR / f"{a.name}.title").write_text((a.title or f"Sub-agent {a.name}") + "\n")
    (DIR / f"{a.name}.exit").unlink(missing_ok=True)
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
        print(f"{n:<28} {state:<9} exit={exit_file.read_text().strip() if exit_file.exists() else '-':<4} "
              f"log={log.stat().st_size if log.exists() else 0}B")
        shown += 1
    if not shown:
        print("no sub-agents")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["__run"]:
        return run(*argv[1:5])
    p = argparse.ArgumentParser(prog="clarp-sub-agent", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("name"); s.add_argument("workdir"); s.add_argument("prompt_file")
    s.add_argument("--model"); s.add_argument("--backend", choices=("claude", "codex"), default="claude")
    s.add_argument("--title")
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

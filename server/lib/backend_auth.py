"""Backend CLI authentication status and headless login tasks.

Credentials remain owned by each CLI.  Clarp only invokes documented CLI
commands and returns bounded, scrubbed human-readable output to the client.
"""
from __future__ import annotations

import json
import re
import queue
import base64
import pathlib
import shutil
import subprocess
import threading
import time
from typing import Any

_lock = threading.Lock()
_tasks: dict[str, dict[str, Any]] = {}
_processes: dict[str, subprocess.Popen] = {}
_code_submitted: dict[str, str] = {}
_validation_cache: dict[str, tuple[float, str, str]] = {}
_secret = re.compile(r"(?i)(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,})")
_ansi_osc = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_ansi_osc_open = re.compile(r"\x1b\].*$", re.DOTALL)
_ansi_csi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ansi_single = re.compile(r"\x1b[@-_]")
_url = re.compile(r"https://[^\s\x1b]+")
_device_code = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{4}-[A-Z0-9]{5}(?![A-Z0-9])")
# `claude auth login` prints the browser URL and then blocks on stdin with
# "Paste code here if prompted > ".  The prompt is the only signal that the CLI
# wants the authorization code the sign-in page shows.
_code_prompt = re.compile(r"(?:paste|enter)[^\n]*code[^\n]*>[ \t]*\Z", re.IGNORECASE)
_code_prompt_prefix = re.compile(
    r"^(?:paste|enter)[^\n]*code[^\n]*>[ \t]*", re.IGNORECASE)
_MAX_CODE_LENGTH = 512
_LOGIN_TIMEOUT_SECONDS = 600
_RAW_OUTPUT_LIMIT = 65_536
_VALIDATION_TTL_SECONDS = 60


def _clean_output(value: str) -> str:
    clean = _ansi_osc.sub("", value or "")
    clean = _ansi_osc_open.sub("", clean)
    clean = _ansi_csi.sub("", clean)
    clean = _ansi_single.sub("", clean)
    clean = "".join(character for character in clean
                    if character in "\n\t" or ord(character) >= 32)
    return _secret.sub("[redacted]", clean)[-8000:].strip()


def _append_output(current: str, character: str) -> str:
    # Retain the newest raw output so late device instructions and failures are
    # visible. Publishing uses only the final 8k, far inside this 64k window,
    # so a secret split at the discarded boundary cannot enter the response.
    return (current + character)[-_RAW_OUTPUT_LIMIT:]


def _failure_summary(value: str) -> str:
    clean = _clean_output(value)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    # The CLI writes its failure on the same line as the unanswered stdin
    # prompt, which would otherwise headline every error.
    summary = _code_prompt_prefix.sub("", lines[-1]) if lines else ""
    return (summary or "Sign-in failed")[:500]


def _task_value(backend: str, state: str, output: str,
                *, started_at: int | None = None, error: str = "",
                submitted_output: str = "") -> dict[str, Any]:
    clean = _clean_output(output)
    verification_url = next((url.rstrip(".,)") for url in _url.findall(clean)
                             if "auth" in url or "login" in url), "")
    code_match = _device_code.search(clean)
    now_ms = int(time.time() * 1000)
    started = started_at or now_ms
    pending = state == "running" and bool(_code_prompt.search(clean))
    return {
        "backend": backend,
        "status": state,
        "output": clean,
        "verification_url": verification_url,
        "user_code": code_match.group(0) if code_match else "",
        # The CLI blocks on stdin until the client posts the code back, so an
        # already-answered prompt must not keep asking for the same paste.
        "awaiting_code": pending and clean != submitted_output,
        "code_submitted": pending and clean == submitted_output,
        "started_at": started,
        "expires_at": (started + _LOGIN_TIMEOUT_SECONDS * 1000
                       if code_match or verification_url else 0),
        "error": error,
    }


def _run(argv: list[str], timeout: float = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                          check=False)


def _jwt_expiry_ms(value: str) -> int:
    try:
        parts = value.split(".")
        if len(parts) != 3:
            return 0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return int(decoded.get("exp") or 0) * 1000
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0


def _credential_metadata(backend: str) -> tuple[bool, int]:
    try:
        if backend == "claude":
            path = pathlib.Path.home() / ".claude/.credentials.json"
            payload = json.loads(path.read_text())
            login = payload.get("claudeAiOauth") or {}
            token = str(login.get("accessToken") or "")
            return bool(token), int(login.get("expiresAt") or 0)
        path = pathlib.Path.home() / ".codex/auth.json"
        payload = json.loads(path.read_text())
        tokens = payload.get("tokens") or {}
        token = str(tokens.get("access_token") or payload.get("OPENAI_API_KEY") or "")
        return bool(token), _jwt_expiry_ms(token)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, 0


def _validation(backend: str, *, force: bool = False) -> tuple[str, str]:
    now = time.monotonic()
    with _lock:
        cached = _validation_cache.get(backend)
    if cached and not force and now - cached[0] < _VALIDATION_TTL_SECONDS:
        return cached[1], cached[2]
    try:
        from . import backend_usage
        if backend == "claude":
            backend_usage.fetch_claude_usage(timeout=6)
        else:
            backend_usage.fetch_codex_usage(timeout=6)
        state, error = "valid", ""
    except Exception as exc:  # noqa: BLE001 — provider errors become bounded state
        error = _failure_summary(str(exc))
        lower = error.lower()
        if "expired" in lower:
            state = "expired"
        elif any(value in lower for value in (
                "auth failed", "http 401", "http 403", "credentials unavailable",
                "access token unavailable", "access token missing")):
            state = "invalid"
        else:
            state = "unverified"
    with _lock:
        _validation_cache[backend] = (now, state, error)
    return state, error


def status(*, validate: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    claude = shutil.which("claude")
    if claude:
        try:
            result = _run([claude, "auth", "status", "--json"])
            data = json.loads(result.stdout) if result.stdout.strip() else {}
            present, expires_at = _credential_metadata("claude")
            locally_logged_in = result.returncode == 0 and bool(data.get("loggedIn"))
            cli_error = _failure_summary(result.stderr) if result.returncode != 0 else ""
            state, validation_error = "signed_out", ""
            if locally_logged_in:
                if expires_at and expires_at <= int(time.time() * 1000):
                    state = "expired"
                elif validate:
                    state, validation_error = _validation("claude")
                else:
                    state = "unverified"
            elif present:
                state = "error" if cli_error else "invalid"
            rows.append({
                "id": "claude", "name": "Claude", "installed": True,
                "logged_in": state == "valid",
                "account": data.get("email") or "",
                "method": data.get("authMethod") or data.get("apiProvider") or "",
                "state": state, "credential_present": present,
                "can_logout": present, "expires_at": expires_at,
                "validation_error": validation_error,
                "error": cli_error,
            })
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            rows.append({"id": "claude", "name": "Claude", "installed": True,
                         "logged_in": False, "error": str(exc)})
    else:
        rows.append({"id": "claude", "name": "Claude", "installed": False,
                     "logged_in": False})

    codex = shutil.which("codex")
    if codex:
        try:
            result = _run([codex, "login", "status"])
            output = (result.stdout + result.stderr).strip()
            present, expires_at = _credential_metadata("codex")
            locally_logged_in = result.returncode == 0 and "logged in" in output.lower()
            state, validation_error = "signed_out", ""
            if locally_logged_in:
                if expires_at and expires_at <= int(time.time() * 1000):
                    state = "expired"
                elif validate:
                    state, validation_error = _validation("codex")
                else:
                    state = "unverified"
            elif present:
                state = "error" if output else "invalid"
            rows.append({
                "id": "codex", "name": "Codex", "installed": True,
                "logged_in": state == "valid",
                "method": output.removeprefix("Logged in using ").strip()
                          if result.returncode == 0 else "",
                "state": state, "credential_present": present,
                "can_logout": present, "expires_at": expires_at,
                "validation_error": validation_error,
                "error": "" if result.returncode == 0 else _failure_summary(output),
            })
        except (OSError, subprocess.SubprocessError) as exc:
            rows.append({"id": "codex", "name": "Codex", "installed": True,
                         "logged_in": False, "error": str(exc)})
    else:
        rows.append({"id": "codex", "name": "Codex", "installed": False,
                     "logged_in": False})
    return rows


def task(backend: str) -> dict[str, Any]:
    with _lock:
        return dict(_tasks.get(backend, _task_value(backend, "idle", "")))


def start_login(backend: str) -> dict[str, Any]:
    executable = shutil.which(backend)
    if backend not in {"claude", "codex"} or executable is None:
        raise ValueError(f"{backend} CLI is not installed")
    with _lock:
        _validation_cache.pop(backend, None)
        current = _tasks.get(backend)
        if current and current.get("status") == "running":
            return dict(current)
        _code_submitted.pop(backend, None)
        _processes.pop(backend, None)
        started_at = int(time.time() * 1000)
        _tasks[backend] = _task_value(
            backend, "running", "", started_at=started_at)

    argv = ([executable, "login", "--device-auth"] if backend == "codex"
            else [executable, "auth", "login", "--claudeai"])

    def worker() -> None:
        try:
            process = subprocess.Popen(
                argv, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
            )
            with _lock:
                _processes[backend] = process
            output = ""
            deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
            assert process.stdout is not None
            chunks: queue.Queue[str | None] = queue.Queue()

            def drain() -> None:
                while char := process.stdout.read(1):
                    chunks.put(char)
                chunks.put(None)

            threading.Thread(target=drain, daemon=True,
                             name=f"backend-login-output-{backend}").start()
            stream_done = False
            while process.poll() is None or not stream_done:
                try:
                    char = chunks.get(timeout=0.2)
                except queue.Empty:
                    char = ""
                if char is None:
                    stream_done = True
                elif char:
                    output = _append_output(output, char)
                    with _lock:
                        _tasks[backend] = _task_value(
                            backend, "running", output, started_at=started_at,
                            submitted_output=_code_submitted.get(backend, ""))
                if time.monotonic() >= deadline and process.poll() is None:
                    process.kill()
                    output += "\nLogin timed out."
                    deadline = float("inf")
            returncode = process.wait(timeout=5)
            state = "complete" if returncode == 0 else "failed"
        except OSError as exc:
            output, state = str(exc), "failed"
        with _lock:
            _processes.pop(backend, None)
            _code_submitted.pop(backend, None)
            _tasks[backend] = _task_value(
                backend, state, output, started_at=started_at,
                error="" if state == "complete" else _failure_summary(output))
            _validation_cache.pop(backend, None)

    threading.Thread(target=worker, daemon=True,
                     name=f"backend-login-{backend}").start()
    return task(backend)


def submit_login_code(backend: str, code: str) -> dict[str, Any]:
    """Hand the authorization code the sign-in page showed back to the CLI.

    `claude auth login` cannot be answered out of band: the code has to reach
    the running process on stdin, so the client posts it here.
    """
    if backend not in {"claude", "codex"}:
        raise ValueError(f"{backend} CLI is not installed")
    value = str(code or "").strip()
    if not value:
        raise ValueError("Authorization code is required")
    if len(value) > _MAX_CODE_LENGTH or "\n" in value or "\r" in value:
        raise ValueError("Authorization code is not in the expected format")
    with _lock:
        current = _tasks.get(backend)
        process = _processes.get(backend)
        if not current or current.get("status") != "running" or process is None:
            raise RuntimeError("No sign-in is waiting for a code")
        if not current.get("awaiting_code"):
            raise RuntimeError("This sign-in is not asking for a code")
        stdin = process.stdin
        if stdin is None or stdin.closed:
            raise RuntimeError("Sign-in is no longer accepting input")
        try:
            stdin.write(value + "\n")
            stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not send the code: {exc}") from exc
        # The prompt stays in the buffer after the CLI reads stdin, so remember
        # what was answered instead of asking for the same paste again.
        _code_submitted[backend] = current.get("output", "")
        _tasks[backend] = _task_value(
            backend, "running", current.get("output", ""),
            started_at=current.get("started_at"),
            submitted_output=_code_submitted[backend])
        return dict(_tasks[backend])


def logout(backend: str) -> dict[str, Any]:
    executable = shutil.which(backend)
    if backend not in {"claude", "codex"} or executable is None:
        raise ValueError(f"{backend} CLI is not installed")
    argv = ([executable, "auth", "logout"] if backend == "claude"
            else [executable, "logout"])
    result = _run(argv, timeout=30)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(_failure_summary(output))
    with _lock:
        _tasks.pop(backend, None)
        _code_submitted.pop(backend, None)
        _validation_cache.pop(backend, None)
    return next(row for row in status(validate=False) if row["id"] == backend)

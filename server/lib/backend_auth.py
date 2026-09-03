"""Backend CLI authentication status and headless login tasks.

Credentials remain owned by each CLI.  Clarp only invokes documented CLI
commands and returns bounded, scrubbed human-readable output to the client.

Which CLIs get a sign-in row is decided by the backend registry: every
adapter with ``login_kind != "none"`` is listed, in registry order, and the
CLI-specific commands live in ``_DRIVERS`` here. A new backend therefore
appears in Settings the moment its adapter declares a login kind and a
driver is added below; no client change is needed.
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
from dataclasses import dataclass
from typing import Any, Callable

from . import backends

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


@dataclass(frozen=True)
class _StatusReading:
    """What one ``status_argv`` run said about the local sign-in."""
    logged_in: bool
    account: str = ""
    method: str = ""
    error: str = ""


@dataclass(frozen=True)
class _AuthDriver:
    """The documented commands one CLI exposes for sign-in."""
    status_argv: tuple[str, ...]
    login_argv: tuple[str, ...]
    logout_argv: tuple[str, ...]
    read_status: Callable[[subprocess.CompletedProcess[str]], _StatusReading]
    credential_metadata: Callable[[], tuple[bool, int]]
    validate: Callable[[], None]


def _claude_status(result: subprocess.CompletedProcess[str]) -> _StatusReading:
    data = json.loads(result.stdout) if result.stdout.strip() else {}
    return _StatusReading(
        logged_in=result.returncode == 0 and bool(data.get("loggedIn")),
        account=str(data.get("email") or ""),
        method=str(data.get("authMethod") or data.get("apiProvider") or ""),
        error=_failure_summary(result.stderr) if result.returncode != 0 else "",
    )


def _claude_credentials() -> tuple[bool, int]:
    path = pathlib.Path.home() / ".claude/.credentials.json"
    payload = json.loads(path.read_text())
    login = payload.get("claudeAiOauth") or {}
    token = str(login.get("accessToken") or "")
    return bool(token), int(login.get("expiresAt") or 0)


def _claude_validate() -> None:
    from . import backend_usage
    backend_usage.fetch_claude_usage(timeout=6)


def _codex_status(result: subprocess.CompletedProcess[str]) -> _StatusReading:
    output = (result.stdout + result.stderr).strip()
    logged_in = result.returncode == 0 and "logged in" in output.lower()
    return _StatusReading(
        logged_in=logged_in,
        method=(output.removeprefix("Logged in using ").strip()
                if result.returncode == 0 else ""),
        # A non-zero exit with output is a configuration failure worth
        # showing; "Not logged in" alone is a plain signed-out state.
        error="" if result.returncode == 0 else _failure_summary(output),
    )


def _codex_credentials() -> tuple[bool, int]:
    path = pathlib.Path.home() / ".codex/auth.json"
    payload = json.loads(path.read_text())
    tokens = payload.get("tokens") or {}
    token = str(tokens.get("access_token") or payload.get("OPENAI_API_KEY") or "")
    return bool(token), _jwt_expiry_ms(token)


def _codex_validate() -> None:
    from . import backend_usage
    backend_usage.fetch_codex_usage(timeout=6)


_DRIVERS: dict[str, _AuthDriver] = {
    backends.CLAUDE: _AuthDriver(
        status_argv=("auth", "status", "--json"),
        login_argv=("auth", "login", "--claudeai"),
        logout_argv=("auth", "logout"),
        read_status=_claude_status,
        credential_metadata=_claude_credentials,
        validate=_claude_validate,
    ),
    backends.CODEX: _AuthDriver(
        status_argv=("login", "status"),
        login_argv=("login", "--device-auth"),
        logout_argv=("logout",),
        read_status=_codex_status,
        credential_metadata=_codex_credentials,
        validate=_codex_validate,
    ),
}


def _driver(backend: str) -> _AuthDriver | None:
    adapter = backends.get(backend)
    if adapter is None or not adapter.supports_auth:
        return None
    return _DRIVERS.get(adapter.id)


def _executable(backend: str) -> str | None:
    adapter = backends.get(backend)
    if adapter is None:
        return None
    binary = adapter.required_binary
    if adapter.id == backends.CLAUDE:
        from . import clarp_runner
        binary = clarp_runner.configured_claude_bin()
    return shutil.which(binary)


def _credential_metadata(backend: str) -> tuple[bool, int]:
    driver = _driver(backend)
    if driver is None:
        return False, 0
    try:
        return driver.credential_metadata()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, 0


def _validation(backend: str, *, force: bool = False) -> tuple[str, str]:
    now = time.monotonic()
    with _lock:
        cached = _validation_cache.get(backend)
    if cached and not force and now - cached[0] < _VALIDATION_TTL_SECONDS:
        return cached[1], cached[2]
    try:
        driver = _driver(backend)
        if driver is None:
            raise RuntimeError(f"{backend} has no sign-in validation")
        driver.validate()
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
    """One row per registry adapter that has a sign-in, in registry order."""
    rows: list[dict[str, Any]] = []
    for adapter in backends.auth_adapters():
        driver = _DRIVERS.get(adapter.id)
        if driver is None:
            continue
        base = {"id": adapter.id, "name": adapter.label,
                "login_kind": adapter.login_kind}
        executable = _executable(adapter.id)
        if executable is None:
            rows.append({**base, "installed": False, "logged_in": False})
            continue
        try:
            result = _run([executable, *driver.status_argv])
            reading = driver.read_status(result)
            present, expires_at = _credential_metadata(adapter.id)
            state, validation_error = "signed_out", ""
            if reading.logged_in:
                if expires_at and expires_at <= int(time.time() * 1000):
                    state = "expired"
                elif validate:
                    state, validation_error = _validation(adapter.id)
                else:
                    state = "unverified"
            elif present:
                state = "error" if reading.error else "invalid"
            rows.append({
                **base, "installed": True,
                "logged_in": state == "valid",
                "account": reading.account,
                "method": reading.method,
                "state": state, "credential_present": present,
                "can_logout": present, "expires_at": expires_at,
                "validation_error": validation_error,
                "error": reading.error,
            })
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            rows.append({**base, "installed": True, "logged_in": False,
                         "error": str(exc)})
    return rows


def task(backend: str) -> dict[str, Any]:
    with _lock:
        return dict(_tasks.get(backend, _task_value(backend, "idle", "")))


def start_login(backend: str) -> dict[str, Any]:
    driver = _driver(backend)
    executable = _executable(backend)
    if driver is None or executable is None:
        raise ValueError(f"{backend} CLI is not installed")
    backend = backends.normalize(backend)
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

    argv = [executable, *driver.login_argv]

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
    if _driver(backend) is None:
        raise ValueError(f"{backend} CLI is not installed")
    backend = backends.normalize(backend)
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
    driver = _driver(backend)
    executable = _executable(backend)
    if driver is None or executable is None:
        raise ValueError(f"{backend} CLI is not installed")
    backend = backends.normalize(backend)
    argv = [executable, *driver.logout_argv]
    result = _run(argv, timeout=30)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(_failure_summary(output))
    with _lock:
        _tasks.pop(backend, None)
        _code_submitted.pop(backend, None)
        _validation_cache.pop(backend, None)
    return next(row for row in status(validate=False) if row["id"] == backend)

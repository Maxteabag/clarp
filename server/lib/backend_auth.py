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
from .clock import now_ms as _now_ms
from .log import log_exception

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
    now_ms = _now_ms()
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
    # Listed by a plain ``GET /backend-auth``. Rows added with
    # ``harness_model_providers`` appear only when the client asks for every
    # harness (``?harnesses=all``), so an older app never sees a row it was
    # not built for.
    listed_by_default: bool = True


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


def _grok_status(result: subprocess.CompletedProcess[str]) -> _StatusReading:
    # ``grok models`` has no status command beside it; signed out, it prints
    # "You are not authenticated." and still exits 0.
    output = (result.stdout + result.stderr).strip()
    signed_out = "not authenticated" in output.lower()
    return _StatusReading(
        logged_in=result.returncode == 0 and not signed_out,
        error="" if result.returncode == 0 else _failure_summary(output),
    )


def _no_credential_file() -> tuple[bool, int]:
    # The CLI keeps its credential where Clarp cannot read it; the status
    # command is the evidence.
    return False, 0


def _status_is_validation() -> None:
    return None


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
    backends.GROK: _AuthDriver(
        status_argv=("models",),
        login_argv=("login", "--device-auth"),
        logout_argv=("logout",),
        read_status=_grok_status,
        credential_metadata=_no_credential_file,
        validate=_status_is_validation,
        listed_by_default=False,
    ),
}


def _driver(backend: str) -> _AuthDriver | None:
    adapter = backends.get(backend)
    if adapter is None or not adapter.supports_auth:
        return None
    return _DRIVERS.get(adapter.id)


def _executable(backend: str) -> str | None:
    if backends.get(backend) is None:
        return None
    return shutil.which(backends.by_id(backend).executable())


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


def status(*, validate: bool = True, every_harness: bool = False,
           only: str | None = None) -> list[dict[str, Any]]:
    """One row per registry adapter that has a sign-in, in registry order.

    ``every_harness`` adds a row for every other harness the Host runs: its
    installed state, what Clarp can tell about its sign-in, and whether the
    app can start one (``can_login``). Without it the response is what it was
    before ``harness_model_providers``.
    """
    rows: list[dict[str, Any]] = []
    for adapter in backends.all_adapters() if every_harness else backends.auth_adapters():
        if only is not None and adapter.id != only:
            continue
        driver = _DRIVERS.get(adapter.id) if adapter.supports_auth else None
        if driver is None or (not every_harness and not driver.listed_by_default):
            if every_harness and not adapter.hidden:
                rows.append(_harness_row(adapter))
            continue
        base = {"id": adapter.id, "name": adapter.label,
                "login_kind": adapter.login_kind}
        if every_harness:
            base.update(can_login=True, model_providers=False, note="")
        executable = _executable(adapter.id)
        if executable is None:
            rows.append({**base, "installed": False, "logged_in": False})
            continue
        try:
            result = _run([executable, *driver.status_argv])
            reading = driver.read_status(result)
            present, expires_at = _credential_metadata(adapter.id)
            present = present or reading.logged_in
            state, validation_error = "signed_out", ""
            if reading.logged_in:
                if expires_at and expires_at <= _now_ms():
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


def _agy_account() -> str:
    try:
        payload = json.loads((pathlib.Path.home() / ".gemini/google_accounts.json").read_text())
    except (OSError, ValueError):
        return ""
    return str(payload.get("active") or "") if isinstance(payload, dict) else ""


def _agy_signed_in(executable: str) -> tuple[bool, str]:
    """``agy`` has no status command; listing its account-scoped models is the
    evidence. Signing in is the CLI's own interactive flow on the Host."""
    try:
        result = _run([executable, "models"], timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, _failure_summary(str(exc))
    lines = [line for line in result.stdout.splitlines()
             if line.strip() and not line.lower().startswith("fetching")]
    if result.returncode == 0 and lines:
        return True, ""
    return False, "" if result.returncode == 0 else _failure_summary(result.stdout + result.stderr)


# Harnesses whose sign-in Clarp can read but not drive: (state reader, note).
_STATUS_ONLY: dict[str, tuple[Callable[[str], tuple[bool, str]], Callable[[], str], str]] = {
    backends.AGY: (_agy_signed_in, _agy_account,
                   "Sign in by running agy on the Host."),
}


def _harness_row(adapter: Any) -> dict[str, Any]:
    """A harness without a sign-in Clarp drives: what can be said about it."""
    executable = _executable(adapter.id)
    row: dict[str, Any] = {
        "id": adapter.id, "name": adapter.label, "login_kind": adapter.login_kind,
        "installed": executable is not None, "logged_in": False,
        "account": "", "method": "", "state": "signed_out",
        "credential_present": False, "can_logout": False, "expires_at": 0,
        "validation_error": "", "error": "",
        "can_login": False, "model_providers": bool(adapter.model_providers), "note": "",
    }
    if executable is None:
        return row
    if adapter.model_providers:
        from . import harness_providers
        listing = harness_providers.providers(adapter.id, refresh_methods=False)
        family = adapter.model_id_filter
        # A one-family card (DeepSeek) is usable only through a provider
        # that serves that family.
        signed = [p for p in listing["providers"] if p["signed_in"] and (
            not family or harness_providers.hosts_family(p["id"], family))]
        row.update(
            logged_in=bool(signed), state="valid" if signed else "signed_out",
            credential_present=bool(signed),
            account=", ".join(p["name"] for p in signed[:3]) + (
                f" and {len(signed) - 3} more" if len(signed) > 3 else ""),
            model_providers_signed_in=[{"id": p["id"], "name": p["name"],
                                        "credential": p["credential"]} for p in signed],
            note=("Sign in to a model provider to use it."
                  if not signed else f"{len(signed)} model provider"
                  + ("s" if len(signed) != 1 else "") + " signed in."),
        )
        return row
    reader = _STATUS_ONLY.get(adapter.id)
    if reader is None:
        row.update(state="host_managed",
                   note=f"Sign in to {adapter.label} on the Host.")
        return row
    read_state, read_account, note = reader
    signed_in, error = read_state(executable)
    row.update(logged_in=signed_in, state="valid" if signed_in else "signed_out",
               credential_present=signed_in, error=error, note=note,
               account=read_account() if signed_in else "")
    return row


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
        started_at = _now_ms()
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
        if state == "complete":
            backends.by_id(backend).on_credential_change()

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
    backends.by_id(backend).on_credential_change()
    return next(row for row in status(validate=False, every_harness=True, only=backend)
                if row["id"] == backend)

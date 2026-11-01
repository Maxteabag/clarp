"""Model providers inside a harness that can use many of them (OpenCode).

A harness is the agent CLI that runs a turn (Claude Code, Codex, OpenCode…).
OpenCode can serve models from many providers (OpenRouter, Hugging Face,
DeepSeek, Anthropic…), each with its own sign-in. This module lets the apps
list those providers, sign in with an API key or the provider's own device
flow, sign out, and list a provider's models.

Credentials stay in OpenCode's own store (``$XDG_DATA_HOME/opencode/auth.json``,
0600). An API key is written there directly; a device or OAuth sign-in runs
through a short-lived ``opencode serve``, which writes the store itself. A key
is never logged, never returned to a client and never kept in memory past the
write: rows carry only the credential's type.

The provider list comes from OpenCode's models.dev cache, so listing never
starts the CLI. Which providers offer an OAuth or device sign-in is only known
to OpenCode's plugins (``GET /provider/auth`` on ``opencode serve``); that answer
is remembered per OpenCode version and refreshed in the background.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from . import backends, settings_store
from .clock import now_ms as _now_ms
from .log import log, log_exception

HARNESSES = (backends.OPENCODE, backends.DEEPSEEK)
STORE_HARNESS = backends.OPENCODE
LOGIN_TIMEOUT_SECONDS = 600
METHODS_TTL_SECONDS = 24 * 3600
MAX_KEY_LENGTH = 4096
MAX_CODE_LENGTH = 512
_METHODS_KEY = "harness.opencode.provider_auth_methods"
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_USER_CODE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{4,5}-[A-Z0-9]{4,5}(?![A-Z0-9])")
# The providers most people reach for, shown before the long tail of the
# models.dev catalogue. Signed-in providers always come first.
POPULAR = (
    "openrouter", "anthropic", "openai", "deepseek", "huggingface",
    "fireworks-ai", "groq", "github-copilot", "xai", "google", "mistral",
    "togetherai", "cerebras",
)

_lock = threading.RLock()
_tasks: dict[str, dict[str, Any]] = {}
_sessions: dict[str, "_LoginSession"] = {}
_methods_refresh: threading.Thread | None = None


class ProviderError(ValueError):
    """A bad request: unknown harness or provider, malformed key or code."""


class ProviderConflict(RuntimeError):
    """The request is valid but cannot run now (no sign-in waiting, CLI missing)."""


# --- paths ---------------------------------------------------------------

def _xdg(var: str, default: str) -> pathlib.Path:
    value = os.environ.get(var, "").strip()
    return pathlib.Path(value) if value else pathlib.Path.home() / default


def auth_path() -> pathlib.Path:
    """OpenCode's credential store, resolved the way OpenCode resolves it."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "auth.json"


def catalogue_path() -> pathlib.Path:
    """OpenCode's models.dev cache."""
    return _xdg("XDG_CACHE_HOME", ".cache") / "opencode" / "models.json"


def _executable() -> str | None:
    return shutil.which(backends.by_id(STORE_HARNESS).executable())


def harness_id(harness: str | None) -> str:
    """The harness whose store a request means; the DeepSeek card is OpenCode."""
    value = (harness or STORE_HARNESS).strip().lower()
    if not backends.is_valid(value) or backends.normalize(value) not in HARNESSES:
        raise ProviderError(f"{harness or value} has no model providers")
    return backends.normalize(value)


def _provider_id(provider: Any) -> str:
    value = str(provider or "").strip().lower()
    if not _PROVIDER_ID.fullmatch(value):
        raise ProviderError("provider is required")
    return value


# --- OpenCode's stores ---------------------------------------------------

def read_catalogue() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(catalogue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _read_auth() -> dict[str, Any]:
    try:
        data = json.loads(auth_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # Never echo the file: it holds secrets.
        raise ProviderConflict("OpenCode's credential store could not be read") from exc
    return data if isinstance(data, dict) else {}


def _write_auth(data: dict[str, Any]) -> None:
    """Replace the store atomically, 0600 in a 0700 directory."""
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def credential_types() -> dict[str, str]:
    """provider id -> credential type ("api", "oauth", "wellknown"); no secrets."""
    try:
        data = _read_auth()
    except ProviderConflict:
        return {}
    return {str(k): str(v.get("type") or "") for k, v in data.items() if isinstance(v, dict)}


# --- sign-in methods (remembered from ``opencode serve``) ------------------

def _cli_version(executable: str) -> str:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()[:100]


def _remembered_methods() -> tuple[dict[str, list[dict[str, Any]]], str, float]:
    try:
        payload = json.loads(settings_store.get_text(_METHODS_KEY) or "{}")
    except Exception:  # noqa: BLE001 - a missing answer only hides OAuth buttons
        return {}, "", 0.0
    if not isinstance(payload, dict) or not isinstance(payload.get("methods"), dict):
        return {}, "", 0.0
    return payload["methods"], str(payload.get("version") or ""), float(payload.get("at") or 0)


def usable_methods(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """OpenCode's ``/provider/auth`` reduced to what a phone can complete.

    A method keeps its index in OpenCode's list (the authorize call names it by
    index). Methods that wait for a browser redirect to localhost on the Host
    ("(browser)", "External Browser") or for a local CLI (Azure CLI) cannot be
    finished from another device and are left out.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for provider, methods in (raw or {}).items():
        if not isinstance(methods, list):
            continue
        kept = []
        for index, method in enumerate(methods):
            if not isinstance(method, dict) or method.get("type") != "oauth":
                continue
            label = str(method.get("label") or "")
            lowered = label.lower()
            if "browser" in lowered or "azure cli" in lowered:
                continue
            prompts = [p for p in method.get("prompts") or [] if isinstance(p, dict)]
            kept.append({"index": index, "type": "oauth", "label": label,
                         "prompts": prompts})
        if kept:
            out[str(provider)] = kept
    return out


def _probe_methods(executable: str) -> dict[str, Any]:
    with _ServeProcess(executable) as serve:
        return serve.client.request("GET", "/provider/auth", timeout=20) or {}


def refresh_methods_in_background(*, force: bool = False,
                                  probe: Callable[[str], dict[str, Any]] | None = None) -> None:
    """Remember OpenCode's sign-in methods; one probe at a time."""
    global _methods_refresh
    executable = _executable()
    if executable is None:
        return
    _, version, at = _remembered_methods()
    if not force and at and time.time() - at < METHODS_TTL_SECONDS:
        return
    run_probe = probe or _probe_methods

    def work() -> None:
        global _methods_refresh
        try:
            current = _cli_version(executable)
            raw = run_probe(executable)
            if isinstance(raw, dict):
                settings_store.set_text(_METHODS_KEY, json.dumps(
                    {"version": current, "at": time.time(),
                     "methods": usable_methods(raw)}, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 - OAuth buttons stay hidden
            log_exception("harnessProviderMethodsProbeFail", exc)
        finally:
            with _lock:
                _methods_refresh = None

    with _lock:
        if _methods_refresh is not None and _methods_refresh.is_alive():
            return
        _methods_refresh = threading.Thread(target=work, daemon=True,
                                            name="harness-provider-methods")
        _methods_refresh.start()


# --- listing ---------------------------------------------------------------

def _credential(provider_id: str, entry: dict[str, Any], types: dict[str, str]) -> str | None:
    if provider_id in types:
        return types[provider_id] or "api"
    if any(os.environ.get(str(var)) for var in entry.get("env") or ()):
        return "env"
    return None


def _row(provider_id: str, entry: dict[str, Any], types: dict[str, str],
         methods: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    credential = _credential(provider_id, entry, types)
    models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
    sign_in = [{"index": -1, "type": "api", "label": "API key", "prompts": []}]
    sign_in += methods.get(provider_id, [])
    return {
        "id": provider_id,
        "name": str(entry.get("name") or provider_id),
        "doc": str(entry.get("doc") or ""),
        "env": [str(v) for v in entry.get("env") or ()],
        "model_count": len(models),
        "popular": provider_id in POPULAR,
        "signed_in": credential is not None,
        "credential": credential,
        # A key from the Host's environment is not Clarp's to remove.
        "can_logout": credential is not None and credential != "env",
        "methods": sign_in,
    }


def providers(harness: str | None = None) -> dict[str, Any]:
    harness = harness_id(harness)
    catalogue = read_catalogue()
    types = credential_types()
    methods, _, _ = _remembered_methods()
    refresh_methods_in_background()
    rows = [_row(pid, entry, types, methods) for pid, entry in catalogue.items()]
    # A credential for a provider the catalogue does not list (a custom
    # provider from opencode.json) is still shown so it can be signed out.
    for pid in types:
        if pid not in catalogue and _PROVIDER_ID.fullmatch(pid):
            rows.append(_row(pid, {"name": pid}, types, methods))
    if harness == backends.DEEPSEEK:
        rows = [row for row in rows if _hosts_deepseek(catalogue.get(row["id"]) or {})
                or row["signed_in"]]
    popular_rank = {pid: i for i, pid in enumerate(POPULAR)}
    rows.sort(key=lambda row: (not row["signed_in"], not row["popular"],
                               popular_rank.get(row["id"], 0), row["name"].lower()))
    with _lock:
        tasks = {pid: dict(task) for pid, task in _tasks.items()}
    return {
        "harness": harness,
        "store_harness": STORE_HARNESS,
        "installed": _executable() is not None,
        "catalogue_available": bool(catalogue),
        "providers": rows,
        "tasks": tasks,
    }


def _hosts_deepseek(entry: dict[str, Any]) -> bool:
    models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
    return any("deepseek" in str(mid).lower() for mid in models)


def provider_row(provider: str) -> dict[str, Any]:
    provider = _provider_id(provider)
    catalogue = read_catalogue()
    methods, _, _ = _remembered_methods()
    return _row(provider, catalogue.get(provider) or {"name": provider},
                credential_types(), methods)


def provider_name(provider_id: str, catalogue: dict[str, Any] | None = None) -> str:
    entry = (catalogue if catalogue is not None else read_catalogue()).get(provider_id) or {}
    return str(entry.get("name") or provider_id)


def models(harness: str | None, provider: str) -> dict[str, Any]:
    """A provider's models from the catalogue, as ``provider/model`` ids."""
    harness = harness_id(harness)
    provider = _provider_id(provider)
    entry = read_catalogue().get(provider)
    if entry is None:
        raise ProviderError(f"unknown provider {provider}")
    efforts = list(backends.by_id(harness).efforts)
    rows = []
    for model_id, detail in (entry.get("models") or {}).items():
        if harness == backends.DEEPSEEK and "deepseek" not in str(model_id).lower():
            continue
        detail = detail if isinstance(detail, dict) else {}
        variants = detail.get("variants")
        rows.append({
            "id": f"{provider}/{model_id}",
            "label": str(detail.get("name") or model_id),
            "model_provider": provider,
            "supported_efforts": ([e for e in efforts if e in variants]
                                  if isinstance(variants, dict) else None),
        })
    rows.sort(key=lambda row: row["label"].lower())
    return {"harness": harness, "provider": provider,
            "name": str(entry.get("name") or provider), "models": rows}


# --- sign-in and sign-out -------------------------------------------------

def _clean_secret(value: Any, *, limit: int, what: str) -> str:
    if not isinstance(value, str):
        raise ProviderError(f"{what} is required")
    clean = value.strip()
    if not clean:
        raise ProviderError(f"{what} is required")
    if len(clean) > limit or any(ch in clean for ch in "\r\n\x00"):
        raise ProviderError(f"{what} is not in the expected format")
    return clean


def _credentials_changed() -> None:
    """New credentials change which models ``opencode models`` lists."""
    from . import provider_capabilities
    try:
        provider_capabilities.forget_opencode_models()
    except Exception as exc:  # noqa: BLE001 - the listing refreshes on its own TTL
        log_exception("harnessProviderCatalogueRefreshFail", exc)


def save_api_key(harness: str | None, provider: str, key: Any) -> dict[str, Any]:
    harness_id(harness)
    provider = _provider_id(provider)
    secret = _clean_secret(key, limit=MAX_KEY_LENGTH, what="API key")
    with _lock:
        data = _read_auth()
        data[provider] = {"type": "api", "key": secret}
        _write_auth(data)
        _tasks.pop(provider, None)
    log("harnessProviderSignedIn", f"provider={provider} method=api")
    _credentials_changed()
    return provider_row(provider)


def logout(harness: str | None, provider: str) -> dict[str, Any]:
    harness_id(harness)
    provider = _provider_id(provider)
    with _lock:
        data = _read_auth()
        if provider not in data:
            row = provider_row(provider)
            if row["credential"] == "env":
                raise ProviderConflict(
                    f"{row['name']} is signed in through the Host's environment")
            raise ProviderConflict(f"{row['name']} is not signed in")
        data.pop(provider)
        _write_auth(data)
        _tasks.pop(provider, None)
    log("harnessProviderSignedOut", f"provider={provider}")
    _credentials_changed()
    return provider_row(provider)


class _ServeProcess:
    """A private ``opencode serve`` on 127.0.0.1 with a random password."""

    def __init__(self, executable: str):
        from .backend import opencode_serve
        self._password = opencode_serve.server_password()
        env = dict(os.environ, OPENCODE_SERVER_PASSWORD=self._password)
        self.proc = subprocess.Popen(
            opencode_serve.serve_cmd(executable), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
            cwd=str(pathlib.Path.home()), start_new_session=True)
        url = opencode_serve.wait_for_url(self.proc)
        if not url:
            self.close()
            raise ProviderConflict("OpenCode did not start")
        self.client = opencode_serve.ServeClient(url, self._password,
                                                 str(pathlib.Path.home()))

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()  # the supervisor ends the server
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self) -> "_ServeProcess":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class _LoginSession:
    """One running OAuth or device sign-in for one provider."""

    def __init__(self, provider: str, method: int, serve: Any):
        self.provider = provider
        self.method = method
        self.serve = serve
        self.mode = ""
        self.done = threading.Event()
        self.finish: Callable[..., None] = lambda *_args: None


def _task_value(provider: str, status: str, *, started_at: int, method_label: str = "",
                authorization: dict[str, Any] | None = None, error: str = "",
                code_submitted: bool = False) -> dict[str, Any]:
    auth = authorization or {}
    instructions = str(auth.get("instructions") or "")[:500]
    code = _USER_CODE.search(instructions)
    mode = str(auth.get("method") or "")
    return {
        "provider": provider,
        "status": status,
        "method": method_label,
        "verification_url": str(auth.get("url") or "")[:2000],
        "instructions": instructions,
        "user_code": code.group(0) if code else "",
        "awaiting_code": status == "running" and mode == "code" and not code_submitted,
        "code_submitted": status == "running" and code_submitted,
        "started_at": started_at,
        "expires_at": started_at + LOGIN_TIMEOUT_SECONDS * 1000 if status == "running" else 0,
        "error": error[:500],
    }


def _bounded_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    text = re.sub(r"(?i)(sk-[a-z0-9_-]{8,}|bearer\s+\S+|\"(?:key|access|refresh|token)\"\s*:\s*\"[^\"]*\")",
                  "[redacted]", text)
    return text[:300]


def start_oauth(harness: str | None, provider: str, method: Any,
                inputs: Any = None, *,
                serve_factory: Callable[[str], Any] | None = None) -> dict[str, Any]:
    harness_id(harness)
    provider = _provider_id(provider)
    if isinstance(method, bool) or not isinstance(method, int) or method < 0:
        raise ProviderError("method must be a sign-in method index")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and len(v) <= 500
            for k, v in inputs.items()):
        raise ProviderError("inputs must map names to short strings")
    remembered, _, _ = _remembered_methods()
    offered = {m["index"]: m for m in remembered.get(provider, [])}
    if remembered and method not in offered:
        raise ProviderError(f"{provider_name(provider)} has no such sign-in method")
    executable = _executable()
    if executable is None:
        raise ProviderConflict("OpenCode is not installed on this Host")
    label = str((offered.get(method) or {}).get("label") or "")
    with _lock:
        current = _tasks.get(provider)
        if current and current.get("status") == "running":
            return dict(current)
        started_at = _now_ms()
        _tasks[provider] = _task_value(provider, "running", started_at=started_at,
                                       method_label=label)
    factory = serve_factory or _ServeProcess
    serve = None
    try:
        serve = factory(executable)
        authorization = serve.client.request(
            "POST", f"/provider/{provider}/oauth/authorize",
            {"method": method, "inputs": inputs}, timeout=30) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed task
        if serve is not None:
            serve.close()
        with _lock:
            _tasks[provider] = _task_value(provider, "failed", started_at=started_at,
                                           method_label=label, error=_bounded_error(exc))
            return dict(_tasks[provider])
    session = _LoginSession(provider, method, serve)
    session.mode = str(authorization.get("method") or "")

    def finish(state: str, error: str = "") -> None:
        with _lock:
            if session.done.is_set():
                return
            if _sessions.get(provider) is session:
                _sessions.pop(provider, None)
            _tasks[provider] = _task_value(
                provider, state, started_at=started_at, method_label=label,
                authorization=authorization, error=error)
            session.done.set()
        session.serve.close()
        log("harnessProviderSignInFinished", f"provider={provider} status={state}")
        if state == "complete":
            _credentials_changed()

    session.finish = finish
    with _lock:
        _sessions[provider] = session
        _tasks[provider] = _task_value(provider, "running", started_at=started_at,
                                       method_label=label, authorization=authorization)
    log("harnessProviderSignInStarted", f"provider={provider} mode={session.mode}")

    if session.mode == "auto":
        def wait_for_user() -> None:
            try:
                ok = session.serve.client.request(
                    "POST", f"/provider/{provider}/oauth/callback", {"method": method},
                    timeout=LOGIN_TIMEOUT_SECONDS)
                finish("complete" if ok is not False else "failed",
                       "" if ok is not False else "Sign-in was not completed")
            except Exception as exc:  # noqa: BLE001
                finish("failed", _bounded_error(exc))
        threading.Thread(target=wait_for_user, daemon=True,
                         name=f"harness-provider-login-{provider}").start()
    else:
        def expire() -> None:
            if not session.done.wait(LOGIN_TIMEOUT_SECONDS):
                finish("failed", "Sign-in timed out")
        threading.Thread(target=expire, daemon=True,
                         name=f"harness-provider-login-expiry-{provider}").start()
    return task(provider)


def submit_code(harness: str | None, provider: str, code: Any) -> dict[str, Any]:
    harness_id(harness)
    provider = _provider_id(provider)
    value = _clean_secret(code, limit=MAX_CODE_LENGTH, what="Authorization code")
    with _lock:
        session = _sessions.get(provider)
        current = _tasks.get(provider) or {}
        if session is None or current.get("status") != "running":
            raise ProviderConflict("No sign-in is waiting for a code")
        if session.mode != "code":
            raise ProviderConflict("This sign-in is not asking for a code")
        _tasks[provider] = dict(current, awaiting_code=False, code_submitted=True)
    try:
        ok = session.serve.client.request(
            "POST", f"/provider/{provider}/oauth/callback",
            {"method": session.method, "code": value}, timeout=60)
        session.finish("complete" if ok is not False else "failed",
                       "" if ok is not False else "The code was not accepted")
    except Exception as exc:  # noqa: BLE001
        session.finish("failed", _bounded_error(exc))
    return task(provider)


def task(provider: str) -> dict[str, Any]:
    with _lock:
        current = _tasks.get(provider)
        return dict(current) if current else _task_value(provider, "idle", started_at=0)


def reset_for_tests() -> None:
    with _lock:
        for session in list(_sessions.values()):
            session.done.set()
        _sessions.clear()
        _tasks.clear()

"""Versioned, provider-neutral model capability discovery.

``/agent-model-options`` serves the structured catalog built here: per
provider, a list of models with labels, default and supported efforts, and the
provenance of each observation (live CLI probe or bundled fallback).

Discovery is deliberately read-only.  Missing provider evidence remains
``unknown``/``None``; it is never coerced to a negative capability.
"""
from __future__ import annotations

import copy
import concurrent.futures
import os
import datetime as _dt
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import settings_store


SCHEMA_VERSION = 2
CACHE_TTL_SECONDS = 300
_AGY_LAST_CATALOG_KEY = "provider.agy.last_observed_model_ids"


def _provider_ids() -> tuple[str, ...]:
    from . import backends
    return backends.ids()


def _adapter(provider_id: str):
    from . import backends
    return backends.get(provider_id)


def _fallback_model_rows(provider_id: str) -> tuple[tuple[str, str], ...]:
    adapter = _adapter(provider_id)
    return adapter.fallback_models if adapter else ()


def _static_efforts(provider_id: str) -> list[str]:
    adapter = _adapter(provider_id)
    return list(adapter.efforts) if adapter else []

_cache_lock = threading.Lock()
_cache_condition = threading.Condition(_cache_lock)
_cache: tuple[float, dict[str, Any]] | None = None
_refreshing = False
_refresh_generation = 0
_last_error: BaseException | None = None
_last_error_generation = 0

_AGY_MODEL_ID = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")


def _iso_now(now: float | None = None) -> str:
    value = time.time() if now is None else now
    return (
        _dt.datetime.fromtimestamp(value, tz=_dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _source(kind: str, detail: str, observed_at: str, freshness: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "detail": detail,
        "observed_at": observed_at,
        "freshness": freshness,
    }


def _model(
    model_id: str,
    label: str,
    *,
    default_effort: str | None,
    supported_efforts: list[str] | None,
    source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": model_id,
        "label": label,
        "default_effort": default_effort,
        "supported_efforts": supported_efforts,
        "source": source,
    }


def parse_codex_models(raw: str, *, observed_at: str) -> list[dict[str, Any]]:
    """Parse ``codex debug models`` without inventing missing fields."""
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    items = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    source = _source("cli_probe", "codex debug models", observed_at, "fresh")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug_value = item.get("slug")
        if not isinstance(slug_value, str):
            continue
        model_id = slug_value.strip()
        if not model_id or model_id in seen:
            continue
        label_value = item.get("display_name")
        label = label_value.strip() if isinstance(label_value, str) else model_id
        if not label:
            label = model_id

        default_value = item.get("default_reasoning_level")
        default_effort = (
            default_value.strip().lower()
            if isinstance(default_value, str) and default_value.strip()
            else None
        )
        levels = item.get("supported_reasoning_levels")
        supported: list[str] | None
        if levels is None:
            supported = None
        elif isinstance(levels, list):
            parsed_efforts: list[str] = []
            for level in levels:
                if not isinstance(level, dict):
                    continue
                effort_value = level.get("effort")
                if not isinstance(effort_value, str):
                    continue
                effort = effort_value.strip().lower()
                if effort and effort not in parsed_efforts:
                    parsed_efforts.append(effort)
            # A literal empty list is provider evidence that no efforts are
            # supported. A non-empty but wholly malformed list is unknown.
            supported = parsed_efforts if parsed_efforts or not levels else None
        else:
            supported = None
        out.append(_model(
            model_id,
            label,
            default_effort=default_effort,
            supported_efforts=supported,
            source=dict(source),
        ))
        seen.add(model_id)
    return out


def claude_model_cache_path() -> Path:
    """Where the Claude CLI keeps its account state."""
    return Path(os.environ.get("CLAUDE_CONFIG_FILE") or (Path.home() / ".claude.json"))


# The aliases Claude Code's --model resolves itself (its RP table). "default"
# is deliberately absent: the CLI does not know it and would send it to the API
# verbatim ("does not support this model"); an empty pin is the CLI default.
_CLAUDE_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable", "best", "opusplan"})


def _is_dispatchable_claude_model(value: str) -> bool:
    """A model id Claude's --model actually accepts."""
    model_id = value.strip()
    if model_id.lower().endswith("[1m]"):
        model_id = model_id[:-4]
    if model_id in _CLAUDE_ALIASES:
        return True
    return model_id.startswith("claude-")


def claude_model_pin(value: str | None) -> str:
    """The --model value to dispatch for a stored pin, "" for the CLI default.

    "default" was once listed as an alias; the CLI never understood it."""
    model_id = (value or "").strip()
    return "" if model_id.lower() == "default" else model_id


def parse_claude_models(raw: str, *, observed_at: str) -> list[dict[str, Any]]:
    """Read the Claude CLI's cached model options for this account.

    Claude ships no `models` subcommand, but the CLI caches what the signed-in
    account may actually run - including entitlements a bundled list cannot know
    about, such as a preview model. Those caches are the closest thing to a
    machine-readable catalog, so they are read rather than guessed at.
    """
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    source = _source("cli_cache", "claude account model cache", observed_at, "fresh")
    efforts = list(_static_efforts("claude"))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("modelAccessCache", "additionalModelOptionsCache"):
        entries = payload.get(key)
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                model_id, label = entry.strip(), entry.strip()
            elif isinstance(entry, dict):
                raw_id = (entry.get("value") or entry.get("id")
                          or entry.get("model") or "")
                model_id = raw_id.strip() if isinstance(raw_id, str) else ""
                raw_label = (entry.get("label") or entry.get("display_name")
                             or entry.get("name") or "")
                label = raw_label.strip() if isinstance(raw_label, str) else ""
                description = entry.get("description")
                if isinstance(description, str) and description.strip():
                    # "Fable" alone does not say which model it is; the CLI's own
                    # description does ("Fable 5.1 - Most capable ...").
                    label = description.split("\u00b7")[0].strip() or label
            else:
                continue
            if not model_id or model_id in seen:
                continue
            # The cache also carries UI placeholders - an entry like
            # "cc-update-required-1" is an upgrade prompt, not something
            # --model will accept. Only dispatchable ids get through.
            if not _is_dispatchable_claude_model(model_id):
                continue
            out.append(_model(
                model_id,
                label or model_id,
                default_effort=None,
                supported_efforts=list(efforts),
                source=dict(source),
            ))
            seen.add(model_id)
    return out


def parse_agy_models(raw: str, *, observed_at: str) -> list[dict[str, Any]]:
    """Parse current tab-separated AGY output plus older partial rows.

    Current AGY emits ``slug<TAB>display label``.  A lone slug is retained
    with itself as the label, while prose/noise and rows without an id are
    ignored.  This keeps partial catalogs useful without passing a whole
    tab-separated line back as an invalid ``--model`` value.
    """
    source = _source("cli_probe", "agy models", observed_at, "fresh")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "\t" in raw_line:
            raw_id, raw_label = raw_line.split("\t", 1)
            model_id = raw_id.strip()
            label = raw_label.strip() or model_id
        else:
            # Accept an older/partial slug-only row, but not status prose.
            model_id = line
            if any(character.isspace() for character in model_id):
                continue
            label = model_id
        if (not model_id or not _AGY_MODEL_ID.fullmatch(model_id)
                or model_id in seen):
            continue
        out.append(_model(
            model_id,
            label,
            default_effort=None,
            supported_efforts=None,
            source=dict(source),
        ))
        seen.add(model_id)
    return out


def parse_grok_models(raw: str, *, observed_at: str) -> list[dict[str, Any]]:
    """Parse ``grok models`` list output."""
    source = _source("cli_probe", "grok models", observed_at, "fresh")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    efforts = _static_efforts("grok")
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("You are") or stripped.startswith("Default "):
            continue
        if stripped.lower().startswith("available models"):
            continue
        stripped = stripped.lstrip("*-• ").strip()
        stripped = re.sub(r"\s*\(default\)\s*$", "", stripped, flags=re.I)
        model_id = stripped.split()[0] if stripped else ""
        if not model_id or model_id in seen or model_id.lower() in {"available", "models:"}:
            continue
        seen.add(model_id)
        out.append(_model(
            model_id, model_id, default_effort=None,
            supported_efforts=list(efforts) if efforts else None,
            source=dict(source),
        ))
    return out


def parse_opencode_models(raw: str, *, observed_at: str) -> list[dict[str, Any]]:
    """Parse ``opencode models`` provider/model lines."""
    source = _source("cli_probe", "opencode models", observed_at, "fresh")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    efforts = _static_efforts("opencode")
    for line in (raw or "").splitlines():
        model_id = line.strip()
        if not model_id or model_id.startswith("-") or " " in model_id.split("/")[0]:
            continue
        if "/" not in model_id and not model_id.islower():
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        out.append(_model(
            model_id, model_id, default_effort=None,
            supported_efforts=list(efforts) if efforts else None,
            source=dict(source),
        ))
    return out


def is_agy_model_id(value: Any) -> bool:
    """True only for the strict slug grammar observed from ``agy models``."""
    return isinstance(value, str) and bool(_AGY_MODEL_ID.fullmatch(value.strip()))


def is_dispatchable_agy_model(value: Any) -> bool:
    """Validate against cached observed ids, or bundled ids when unavailable.

    Admission never performs the remote ``agy models`` probe. The catalog
    endpoint owns refresh; settings/spawn use its last observation or the
    explicit bundled fallback so a request cannot block on discovery.
    """
    if not is_agy_model_id(value):
        return False
    model_id = value.strip()
    try:
        with _cache_condition:
            cached = copy.deepcopy(_cache[1]) if _cache is not None else None
        provider = (cached or {}).get("providers", {}).get("agy", {})
        models = provider.get("models") if isinstance(provider, dict) else []
        ids = {
            item.get("id") for item in models or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        source = provider.get("source") if isinstance(provider, dict) else {}
        if isinstance(source, dict) and source.get("kind") == "cli_probe":
            return model_id in ids
    except Exception:
        pass
    persisted: set[str] = set()
    try:
        raw = json.loads(settings_store.get_text(_AGY_LAST_CATALOG_KEY) or "[]")
        if isinstance(raw, list):
            persisted = {item for item in raw if isinstance(item, str)}
    except Exception:  # noqa: BLE001 - persisted observation is best-effort
        pass
    fallback = {item[0] for item in _fallback_model_rows("agy")}
    return model_id in persisted if persisted else model_id in fallback


def _run(argv: list[str], *, timeout: float = 2.5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


def _resolve_executable(provider_id: str) -> str | None:
    """Resolve the same executable name the runtime uses for this provider."""
    binary = provider_id
    if provider_id == "claude":
        # clarp_runner supports a configured Claude-compatible executable.
        # Import lazily so provider discovery remains independent of startup.
        from . import clarp_runner

        binary = clarp_runner.configured_claude_bin()
    return shutil.which(binary)


def _probe_cli_version(
    executable: str,
    observed_at: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str | None, dict[str, Any]]:
    detail = f"{executable} --version"
    try:
        result = run([executable, "--version"], timeout=2.5)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        output = (result.stdout or result.stderr or "").strip()
        version = output.splitlines()[0].strip()[:200] if output else ""
        if version:
            return version, _source("cli_probe", detail, observed_at, "fresh")
    return None, _source("not_observed", detail, observed_at, "unknown")


def _fallback_models(provider_id: str, observed_at: str) -> list[dict[str, Any]]:
    source = _source("static_fallback", "bundled compatibility catalog",
                     observed_at, "unknown")
    efforts = _static_efforts(provider_id) if provider_id == "claude" else None
    return [
        _model(
            model_id,
            label,
            default_effort=None,
            supported_efforts=list(efforts) if efforts is not None else None,
            source=dict(source),
        )
        for model_id, label in _fallback_model_rows(provider_id)
    ]


def _discover_provider(
    provider_id: str,
    observed_at: str,
    *,
    resolve: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    executable = resolve(provider_id)
    installed = executable is not None
    cli_version: str | None = None
    version_source = _source(
        "not_observed", f"{provider_id} executable unavailable",
        observed_at, "unknown")
    models: list[dict[str, Any]] = []
    probe_detail = ""
    if executable:
        # Version and model discovery are independent.  Running them together
        # bounds a cold provider probe by the slower command, not their sum.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            version_future = executor.submit(
                _probe_cli_version, executable, observed_at, run=run)
            if provider_id == "codex":
                probe_detail = "codex debug models"
                try:
                    result = run([executable, "debug", "models"], timeout=2.5)
                    if result.returncode == 0:
                        models = parse_codex_models(
                            result.stdout, observed_at=observed_at)
                except (OSError, subprocess.SubprocessError):
                    pass
            elif provider_id == "claude":
                probe_detail = "claude account model cache"
                try:
                    path = claude_model_cache_path()
                    if path.is_file():
                        models = parse_claude_models(
                            path.read_text(encoding="utf-8", errors="replace"),
                            observed_at=observed_at)
                except OSError:
                    pass
            elif provider_id == "agy":
                probe_detail = "agy models"
                try:
                    # AGY fetches its account-scoped catalog remotely and
                    # routinely takes just under three seconds when healthy.
                    result = run([executable, "models"], timeout=5.0)
                    if result.returncode == 0:
                        models = parse_agy_models(
                            result.stdout, observed_at=observed_at)
                except (OSError, subprocess.SubprocessError):
                    pass
            elif provider_id == "grok":
                probe_detail = "grok models"
                try:
                    result = run([executable, "models"], timeout=5.0)
                    if result.returncode == 0:
                        models = parse_grok_models(
                            result.stdout, observed_at=observed_at)
                except (OSError, subprocess.SubprocessError):
                    pass
            elif provider_id == "opencode":
                probe_detail = "opencode models"
                try:
                    result = run([executable, "models"], timeout=5.0)
                    if result.returncode == 0:
                        models = parse_opencode_models(
                            result.stdout, observed_at=observed_at)
                except (OSError, subprocess.SubprocessError):
                    pass
            cli_version, version_source = version_future.result()

    observed_catalog = bool(models)
    if provider_id == "agy" and observed_catalog:
        try:
            settings_store.set_text(
                _AGY_LAST_CATALOG_KEY,
                json.dumps([item["id"] for item in models], separators=(",", ":")),
            )
        except Exception:  # noqa: BLE001 - discovery remains available
            pass
    if provider_id == "claude":
        known = {item["id"] for item in models}
        models = models + [
            item for item in _fallback_models(provider_id, observed_at)
            if item["id"] not in known
        ]
    elif not models:
        models = _fallback_models(provider_id, observed_at)
    if not installed:
        availability = "unavailable"
    elif provider_id == "claude":
        # The account cache is evidence of entitlement, not of a catalog API.
        availability = "available" if observed_catalog else "unknown"
    else:
        availability = "available" if observed_catalog else "unknown"

    if observed_catalog:
        # Claude's catalog is read from the CLI's account cache, not from a
        # command that enumerates models; say which one it was.
        kind = "cli_cache" if provider_id == "claude" else "cli_probe"
        catalog_source = _source(kind, probe_detail, observed_at, "fresh")
    else:
        catalog_source = _source(
            "static_fallback", "bundled compatibility catalog",
            observed_at, "unknown")
    adapter = _adapter(provider_id)
    # "model": efforts are per model (see each model row); "provider": one
    # list for the whole CLI; "provider_flag": a CLI flag whose compatibility
    # with the chosen model is unknown.
    effort_scope = adapter.effort_scope if adapter else "provider"
    efforts = list(adapter.efforts) if adapter and adapter.efforts else []
    from . import backends
    return {
        "id": provider_id,
        # Presentation and supports_* flags: the client renders these, so a
        # new adapter looks intentional without an app release.
        **backends.catalogue_fields(provider_id),
        "installed": installed,
        "availability": availability,
        "cli_version": cli_version,
        "cli_version_source": version_source,
        # Authentication remains owned by backend_auth in P1.  Joining it into
        # this cached catalog without shared invalidation would imply freshness
        # we do not have, so preserve explicit unknown plus provenance.
        "authenticated": None,
        "authentication_source": _source(
            "not_observed", "backend_auth status is not joined in P1",
            observed_at, "unknown"),
        "supported_efforts": (
            efforts if efforts and effort_scope != "model" else None
        ),
        "supported_efforts_scope": (
            "provider_flag" if effort_scope == "provider_flag" else None
        ),
        "model_effort_compatibility": (
            "unknown" if effort_scope == "provider_flag" else None
        ),
        "models": models,
        "source": catalog_source,
    }


def _build_catalog(
    *,
    wall_time: float,
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    observed_at = _iso_now(wall_time)
    # Provider CLIs are independent.  Probe them concurrently so the endpoint
    # cold path is bounded by the slowest provider rather than all timeouts.
    provider_ids = _provider_ids()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(provider_ids))) as executor:
        futures = {
            provider_id: executor.submit(
                _discover_provider,
                provider_id,
                observed_at,
                resolve=which,
                run=run,
            )
            for provider_id in provider_ids
        }
        providers = {
            provider_id: futures[provider_id].result()
            for provider_id in provider_ids
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "freshness": "fresh",
        "providers": providers,
    }


def capability_catalog(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return one cached catalog, coalescing concurrent refresh callers."""
    global _cache, _refreshing, _refresh_generation
    global _last_error, _last_error_generation
    monotonic_now = time.monotonic()
    with _cache_condition:
        if not force_refresh and _cache is not None:
            cached_at, value = _cache
            if monotonic_now - cached_at <= CACHE_TTL_SECONDS:
                return copy.deepcopy(value)
        if _refreshing:
            waiting_for = _refresh_generation
            while _refreshing and _refresh_generation == waiting_for:
                _cache_condition.wait()
            if _last_error_generation == waiting_for and _last_error is not None:
                raise RuntimeError("provider capability refresh failed") \
                    from _last_error
            if _cache is not None:
                return copy.deepcopy(_cache[1])
        _refreshing = True
        _refresh_generation += 1
        generation = _refresh_generation

    # Never hold the condition lock around provider subprocesses. Whatever
    # happens, the refresh latch is released so waiters can never hang.
    try:
        value = _build_catalog(
            wall_time=time.time(), which=_resolve_executable, run=_run)
        stored_at = time.monotonic()
    except BaseException as error:
        with _cache_condition:
            _last_error = error
            _last_error_generation = generation
            _refreshing = False
            _cache_condition.notify_all()
        raise
    with _cache_condition:
        _cache = (stored_at, value)
        _last_error = None
        _last_error_generation = 0
        _refreshing = False
        _cache_condition.notify_all()
    return copy.deepcopy(value)


def clear_cache() -> None:
    """Clear discovery state, including a refresh in flight.  Public for
    deterministic tests."""
    global _cache, _last_error, _last_error_generation, _refreshing
    with _cache_condition:
        _cache = None
        _last_error = None
        _last_error_generation = 0
        _refreshing = False
        _cache_condition.notify_all()


def endpoint_response(*, force_refresh: bool = False) -> dict[str, Any]:
    """The versioned catalog plus response-level freshness notes."""
    catalog = capability_catalog(force_refresh=force_refresh)
    return {
        **catalog,
        "freshness_scope": "response_observation",
        "freshness_note": (
            "Top-level freshness describes cache age only; inspect each "
            "provider and model source for evidence freshness."
        ),
    }

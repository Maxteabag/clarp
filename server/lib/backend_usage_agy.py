"""Moved out of server/lib/backend_usage.py by tools/refactor/split_apply.py (split 'backend-usage', recipe tools/refactor/splits/s6-backends.toml).
backend_usage.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
from typing import Any
from .clock import now_ms as _now_ms
from .backend_usage_store import AGY, CLAUDE_REFRESH_MS, _decode_raw, _freshness, _iso_to_ms, _normalize_time, _row, _upsert, _window_id


AGY_WINDOW_KINDS = {"5h": ("five_hour", 300), "weekly": ("seven_day", 10080)}


def _agy_executable() -> str | None:
    import shutil
    return shutil.which(os.environ.get("CLARP_AGY_BIN", "agy"))


# One /usage run at a time, and after any attempt (failed ones too) the next
# waits a refresh interval: agy is a 200 MB binary, not an HTTP call.
_AGY_FETCH_LOCK = threading.Lock()
_AGY_LAST_ATTEMPT_MS = [0]


def fetch_agy_usage(*, timeout: float = 20.0) -> dict[str, Any] | None:
    """Antigravity's own quota report (`agy -p /usage`): per model group, a
    weekly and a 5-hour window. It runs no model turn, so it costs nothing.
    Returns None when another refresh is already running."""
    executable = _agy_executable()
    if not executable:
        raise RuntimeError("agy is not installed")
    if not _AGY_FETCH_LOCK.acquire(blocking=False):
        return None
    try:
        _AGY_LAST_ATTEMPT_MS[0] = _now_ms()
        stdout, stderr, returncode = _run_agy_usage(executable, timeout)
    finally:
        _AGY_FETCH_LOCK.release()
    if returncode != 0:
        raise RuntimeError((stderr or stdout or "agy /usage failed").strip()[:300])
    payload = json.loads(stdout)
    groups = (((payload.get("command") or {}).get("data") or {}).get("groups"))
    if not isinstance(groups, list) or not groups:
        raise RuntimeError("agy /usage returned no quota groups")
    buckets = [b for g in groups for b in (g.get("buckets") or [])
               if isinstance(b, dict) and isinstance(b.get("remaining_fraction"), (int, float))]
    # The row's single figure is the tightest window; every window is kept in raw.
    tightest = min(buckets, key=lambda b: b["remaining_fraction"]) if buckets else {}
    used = (round((1 - tightest["remaining_fraction"]) * 100, 1) if tightest else None)
    _upsert(AGY, used_percentage=used, resets_at=str(tightest.get("reset_time") or ""),
            source="agy-usage-command", raw={"groups": groups}, error="")
    return {"groups": groups}


def _run_agy_usage(executable: str, timeout: float) -> tuple[str, str, int]:
    """Run `agy -p /usage` in its own process group, so a timeout also ends any
    helper agy started (one holding the output pipes would hang the wait), and
    in the private HOME Clarp's other agy one-shots use."""
    import signal
    from . import backends
    proc = subprocess.Popen(
        [executable, "--output-format", "json", "-p", "/usage"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(pathlib.Path.home()), start_new_session=True,
        env={**os.environ, **backends.by_id(AGY).routing_env()})
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # agy itself may have answered and exited while a helper it started
        # still holds the pipes: its answer stands.
        finished = proc.poll()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            stdout = stderr = ""
        if finished is None or not stdout:
            raise RuntimeError(f"agy /usage timed out after {timeout:g}s")
        return stdout, stderr or "", finished
    return stdout or "", stderr or "", proc.returncode


def _agy_windows(row: dict[str, Any], provider_instance_id: str, now_ms: int) -> list[dict]:
    """Every group's windows, each labelled with its group: agy's limits are per
    model group (Gemini; Claude and GPT), not per account."""
    freshness = _freshness(row, now_ms)
    observed_at = int(row.get("fetched_at") or 0)
    source = {"kind": "provider_reported", "detail": row.get("source") or "agy-usage-command"}
    windows = []
    for group in _decode_raw(row).get("groups") or []:
        for bucket in group.get("buckets") or []:
            fraction = bucket.get("remaining_fraction")
            if not isinstance(fraction, (int, float)):
                continue
            kind, minutes = AGY_WINDOW_KINDS.get(str(bucket.get("window")), ("unknown", None))
            resets_at = bucket.get("reset_time")
            window_freshness = freshness
            reset_ms = _iso_to_ms(resets_at)
            if reset_ms is not None and now_ms >= reset_ms:
                window_freshness = "stale"
            used = round((1 - float(fraction)) * 100, 1)
            windows.append({
                "window_id": _window_id(
                    provider_instance_id=provider_instance_id,
                    auth_generation_id="agy-usage-command",
                    account_scope_ref=str(bucket.get("id") or group.get("name") or ""),
                    window_kind=kind, window_minutes=minutes,
                    # An unused window's reset slides with the clock; it is
                    # fixed once the window has been used.
                    resets_at=resets_at if used > 0 else None),
                "kind": kind, "scope": "model_group", "label": str(group.get("name") or ""),
                "unit": "percent", "used_percentage": used, "resets_at": resets_at,
                "observed_at": _normalize_time(observed_at / 1000),
                "freshness": window_freshness, "source": source,
                "limit": {"state": "normal" if window_freshness == "fresh" else "unknown",
                          "episode_id": None, "provider_limit_event_id": None},
            })
    return windows


def _agy_needs_refresh() -> bool:
    now = _now_ms()
    if now - _AGY_LAST_ATTEMPT_MS[0] <= CLAUDE_REFRESH_MS:
        return False
    row = _row(AGY)
    return not row or now - int(row.get("fetched_at") or 0) > CLAUDE_REFRESH_MS

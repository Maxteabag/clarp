"""Janitor runtime capability query shared by HTTP and scheduler admission."""
from __future__ import annotations


def runtime_available(ctx) -> bool:
    runtime = getattr(ctx, "runtime_client", None)
    if runtime is None:
        return True
    try:
        return runtime.status().get("capabilities", {}).get("janitor_runs") is True
    except Exception:
        return False


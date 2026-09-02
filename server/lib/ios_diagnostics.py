"""Classification helpers for iOS MetricKit diagnostic payloads."""
from __future__ import annotations

from typing import Any


DIAGNOSTIC_KEYS = {
    "crashes": "crashDiagnostics",
    "hangs": "hangDiagnostics",
    "cpu_exceptions": "cpuExceptionDiagnostics",
    "disk_write_exceptions": "diskWriteExceptionDiagnostics",
    "slow_launches": "appLaunchDiagnostics",
}


def diagnostic_counts(payload: Any) -> dict[str, int]:
    source = payload if isinstance(payload, dict) else {}
    counts = {
        label: len(source.get(key)) if isinstance(source.get(key), list) else 0
        for label, key in DIAGNOSTIC_KEYS.items()
    }
    counts["total"] = sum(counts.values())
    return counts

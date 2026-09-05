#!/usr/bin/env python3
"""Observe AGY lifecycle events without changing its execution decisions."""
import json
import sys

try:
    import _clarp_lib  # noqa: F401
    from lib.agy_hooks import record_event

    record_event(sys.argv[1] if len(sys.argv) > 1 else "", json.load(sys.stdin))
except Exception:
    # An unavailable Clarp install must never prevent terminal work.
    pass

# No permission, context injection, or continuation decisions.
print("{}")

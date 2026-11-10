"""Structural registration only; expected results must come from owner policy.

This helper does not derive a legal target or expected trace from production
edges. Concrete owner tests retain their own independent behavior oracle.
"""
from collections.abc import Mapping


def registered_cases(rules, expected: Mapping[tuple[str | None, str], str | None]):
    pairs = {(state, event) for state in {*rules.states, None} for event in rules.events}
    missing, extra = pairs - expected.keys(), expected.keys() - pairs
    if missing or extra:
        raise AssertionError(f"case registration: missing={missing!r}, extra={extra!r}")
    return tuple(expected.items())

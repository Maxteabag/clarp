"""The refactor program's flake quarantine (tests/quarantine.txt, PT-09).

Stdlib only, imported by conftest. Listed node ids are skipped on local runs so
single-file local runs stay readable; CI (where the gate runs) and
CLARP_RUN_QUARANTINED=1 still run them.
"""
from __future__ import annotations

import pathlib

REASON = "quarantined (refactor program)"
LIST = pathlib.Path(__file__).with_name("quarantine.txt")


def load(path: pathlib.Path = LIST) -> dict[str, str]:
    """Node id -> its note, from a file of `<node id>  # note` lines."""
    entries = {}
    if not path.exists():
        return entries
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        node, _, note = line.partition(" #")
        entries[node.strip()] = note.strip()
    return entries


def active(environ) -> bool:
    return environ.get("CI", "").lower() != "true" and environ.get("CLARP_RUN_QUARANTINED") != "1"


def skip_listed(items, entries, mark) -> int:
    """Add `mark` to every collected item whose node id is listed; return how many."""
    hits = 0
    for item in items:
        if item.nodeid in entries:
            item.add_marker(mark)
            hits += 1
    return hits

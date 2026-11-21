"""What a transcript checkpoint is valid for, and the decision a change to it needs.

``parser_version`` hashes the parser's sources (what a byte range parses to) and
``STORE_FORMAT``; it no longer hashes the store's sources, so a change to a
heartbeat rule or a team block does not re-read every transcript on install.
The store's sources still decide what a full import writes for lines already
imported. This golden holds the digest of both lists and STORE_FORMAT: any change
to a listed file fails here until its author records the decision.

- A parser source changed: ``parser_version`` moves by itself and every
  checkpoint of that backend is refused once (a full read, as it must be).
  Update the golden to say you meant it.
- A store source changed: decide whether a full import now writes different rows,
  or a different store carry, for lines already imported, or whether what a tail
  resumes from changed (``parser_carry``, ``parse_range``, ``_RangePath`` and the
  boundary rules live in transcript_incremental.py, a store source). If so, raise
  ``transcript_incremental.STORE_FORMAT``. That is a deliberate choice with a cost:
  every checkpoint is refused, so every transcript is read again in full on the
  install that ships it. If not, leave it. Then update the golden.

Update with ``UPDATE_TRANSCRIPT_VERSION_GOLDEN=1``; the diff shows the decision.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib

from lib import transcript_incremental as inc

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "transcript-parser-version.json"


def _current():
    return {"store_format": inc.STORE_FORMAT,
            "parse": {backend: inc.sources_digest((*names, *inc._PARSE_COMMON))
                      for backend, names in sorted(inc._VERSION_SOURCES.items())},
            "store": inc.sources_digest(inc._STORE_SOURCES)}


def test_a_change_to_the_parser_or_the_store_is_a_recorded_decision():
    current = _current()
    if os.environ.get("UPDATE_TRANSCRIPT_VERSION_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    moved = [backend for backend in current["parse"] if current["parse"][backend] != golden["parse"].get(backend)]
    assert not moved, (
        f"parser sources changed for {moved}: their checkpoints are refused once after install "
        "(a full read). If that is meant, update the golden (UPDATE_TRANSCRIPT_VERSION_GOLDEN=1).")
    assert current["store"] == golden["store"] and current["store_format"] == golden["store_format"], (
        "a store source changed: if a full import now writes different rows or a different store "
        "carry for lines already imported, or if what a tail resumes from changed (parser_carry, "
        "parse_range, _RangePath, the boundary rules), raise transcript_incremental.STORE_FORMAT. "
        "Raising it is a deliberate choice: every transcript is then read again in full on the "
        "install that ships it. Either way update the golden (UPDATE_TRANSCRIPT_VERSION_GOLDEN=1).")


def test_the_version_follows_the_parser_and_store_format_not_the_store_sources(monkeypatch):
    base = inc.parser_version.__wrapped__("claude")
    monkeypatch.setattr(inc, "_STORE_SOURCES", inc._STORE_SOURCES[:-1])
    assert inc.parser_version.__wrapped__("claude") == base          # a store source: no full read
    monkeypatch.setattr(inc, "STORE_FORMAT", inc.STORE_FORMAT + 1)
    assert inc.parser_version.__wrapped__("claude") != base          # a recorded store change: a full read
    monkeypatch.setattr(inc, "STORE_FORMAT", inc.STORE_FORMAT - 1)
    monkeypatch.setattr(inc, "_PARSE_COMMON", ())
    assert inc.parser_version.__wrapped__("claude") != base          # a parser source: a full read


def test_every_listed_source_exists_and_the_lists_do_not_overlap():
    names = [name for names in inc._VERSION_SOURCES.values() for name in names] + list(inc._PARSE_COMMON)
    root = pathlib.Path(inc.__file__).resolve().parent
    assert all((root / name).is_file() for name in [*names, *inc._STORE_SOURCES])
    assert not set(names) & set(inc._STORE_SOURCES)


# Modules a parser source imports that need not be hashed, each with why.
EXEMPT = {
    "log.py": "logging only",
    "backend/agy_layout.py": "where agy keeps its files: a path only",
    "config.py": "the Host's settings values, not code that parses",
    "settings_store.py": "stored settings values, not code that parses",
    "backend_catalogue.py": "the backends' names and launch defaults",
}


def _imports(name, *, nested):
    """The lib modules ``name`` imports (relative imports), as paths under lib/:
    module-level only, or also those inside functions."""
    root = pathlib.Path(inc.__file__).resolve().parent
    tree = ast.parse((root / name).read_text())
    package = name.rsplit("/", 1)[0] if "/" in name else ""
    nodes = ast.walk(tree) if nested else tree.body
    found = set()
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or node.level < 1:
            continue
        base = package if node.level == 1 else ""
        prefix = base + "/" if base else ""
        if node.module:
            found.add(prefix + node.module.replace(".", "/") + ".py")
        else:
            found.update(prefix + alias.name + ".py" for alias in node.names)
    return {path for path in found if (root / path).is_file()}


def test_every_module_a_parser_imports_is_hashed_or_named_exempt():
    """'A parser change forces a full read' holds by construction: every module a
    transcript parser imports (also inside a function) is in its backend's list or
    in _PARSE_COMMON, and every module those shared helpers import at module level
    is hashed too, or named exempt above with its reason. The backend modules
    (backend/<name>.py) are hashed whole; their imports are the backend's runtime."""
    missing = {}
    for backend, names in inc._VERSION_SOURCES.items():
        allowed = set(names) | set(inc._PARSE_COMMON) | set(EXEMPT)
        for name in names:
            if name.startswith("backend/"):
                continue
            outside = _imports(name, nested=True) - allowed
            if outside:
                missing[f"{backend}:{name}"] = sorted(outside)
    shared = set(inc._PARSE_COMMON) | set(EXEMPT)
    for name in inc._PARSE_COMMON:
        if name.startswith("backend/"):
            continue
        outside = _imports(name, nested=False) - shared
        if outside:
            missing[name] = sorted(outside)
    assert not missing, ("parser sources import modules the parser version does not hash; add them "
                         f"to the backend's list or _PARSE_COMMON, or to EXEMPT with a reason: {missing}")

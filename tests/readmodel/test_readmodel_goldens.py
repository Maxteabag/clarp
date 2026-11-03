"""Golden rows of the transcript import, per backend, as the transcript grows.

Characterization for refactor slice S5 (REFACTOR.md; read-model review §4,
gaps 1-2): the synthetic corpus in ``corpus.py`` goes through the production
import (``transcript_incremental.import_transcript``, the shadow off as on the
live Host) against the seeded database in ``world.py``. After every growth
step the normalized ``messages`` rows, heads and clock must equal the stored
golden, and the parsed turns of the final transcript must equal theirs.

The goldens pin what the code does today, faults included; they are not a
specification. Regenerate them only for a registered intended difference:

    CLARP_READMODEL_GOLDEN_UPDATE=1 uv run --frozen --group dev python -m pytest \
        tests/readmodel/test_readmodel_goldens.py -n 0
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from lib import backends, message_store, message_writes
from lib import transcript_incremental as inc

from tests.readmodel import corpus, world

GOLDENS = pathlib.Path(__file__).with_name("goldens")
UPDATE = os.environ.get("CLARP_READMODEL_GOLDEN_UPDATE") == "1"


@pytest.fixture
def importer(monkeypatch):
    """The import as it runs live: shadow disabled, one write batch (batch
    boundaries follow a 25 ms wall-clock budget, which would make revisions
    depend on machine speed; test_store_txn_characterization pins batching)."""
    monkeypatch.setattr(inc, "settings", lambda: dict(inc._DEFAULTS, enabled=False))
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 0)
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    monkeypatch.setattr(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0)
    clock = world.Clock()
    monkeypatch.setattr(message_writes, "now_ms", clock)
    return clock


def _import(backend: str, transcript: pathlib.Path) -> None:
    inc.import_transcript(backend=backend, path=transcript, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)


def _import_outcome(backend: str, transcript: pathlib.Path) -> str:
    """'ok', or the error the import raised (pinned too: a failing import
    leaves the rows of the previous one)."""
    try:
        _import(backend, transcript)
    except Exception as exc:  # noqa: BLE001 - the golden records it
        return f"{type(exc).__name__}: {exc}"
    return "ok"


def _turns_view(turns: list[dict]) -> list[dict]:
    return json.loads(json.dumps(turns, sort_keys=True, default=str))


def _check(name: str, actual) -> None:
    path = GOLDENS / f"{name}.json"
    text = json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no golden {path.name}; see this module's docstring"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, f"{name} differs from its golden"


@pytest.mark.parametrize("backend", corpus.BACKENDS)
def test_import_rows_after_each_growth_step_match_the_golden(tmp_path, backend, importer):
    world.prepare(tmp_path / "state.sqlite", backend)
    world.seed(backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    steps = []
    for step in corpus.GROWTH:
        corpus.apply(transcript, backend, step)
        importer.ms += 1000
        outcome = _import_outcome(backend, transcript)
        steps.append({"step": step, "import": outcome, **world.snapshot(transcript)})
    final_turns = _turns_view(backends.parse_turns(backend, transcript))
    _check(f"{backend}-store", {"backend": backend, "steps": steps})
    _check(f"{backend}-parse", {"backend": backend, "turns": final_turns})


@pytest.mark.parametrize("backend", corpus.BACKENDS)
def test_a_repeated_import_of_an_unchanged_transcript_writes_nothing(tmp_path, backend, importer):
    world.prepare(tmp_path / "state.sqlite", backend)
    world.seed(backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    for step in ("grow", "complete", "append"):
        corpus.apply(transcript, backend, step)
    _import(backend, transcript)
    before = world.snapshot(transcript)
    importer.ms += 1000
    _import(backend, transcript)
    assert world.snapshot(transcript) == before


def _content(snapshot: dict) -> list[tuple]:
    """What a client renders, without the ids and revisions history decides."""
    return sorted(
        (str(m["seq"]) if m["source_file"] == "<transcript>" else m["source_file"],
         m["role"], m["text"], json.dumps(m["tools_json"], sort_keys=True), m["origin"],
         m["sender_agent_id"], m["trace_id"], m["phase"])
        for m in snapshot["messages"])


@pytest.mark.parametrize("backend", [b for b in corpus.BACKENDS if b != "opencode"])
def test_growth_replay_cut_by_cut_ends_where_one_import_of_the_whole_file_does(
        tmp_path, backend, importer):
    """Grow the file at every record boundary and inside every record (a
    partial last line), importing after each cut; then compare with a fresh
    database that imports the finished file once. Content must agree; the
    rows' per-cut history is pinned by the goldens above."""
    data, cuts = corpus.jsonl_cuts(backend)
    grown_db, fresh_db = tmp_path / "grown.sqlite", tmp_path / "fresh.sqlite"
    transcript = tmp_path / f"{backend}.jsonl"
    world.prepare(grown_db, backend)
    world.seed(backend, importer)
    transcript.write_bytes(b"")
    per_cut = []
    for cut in cuts:
        with open(transcript, "ab") as f:
            f.write(data[f.tell():cut])
        importer.ms += 1000
        _import(backend, transcript)
        per_cut.append(len(world.snapshot(transcript)["messages"]))
    grown = world.snapshot(transcript)
    world.prepare(fresh_db, backend)
    world.seed(backend, importer)
    _import(backend, transcript)
    assert _content(grown) == _content(world.snapshot(transcript))
    _check(f"{backend}-growth-rowcounts", {"backend": backend, "cuts": len(cuts),
                                           "rows_after_cut": per_cut})

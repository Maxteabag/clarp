"""The guard behind explicit checkpoint epochs (design stage 0, review finding 3).

Today a checkpoint is valid only for ``parser_version(backend)``, a hash of
the parser and store sources, so every release that touches them refuses
every checkpoint. The design replaces that hash with an explicit epoch per
backend. An epoch is safe only if a checkpoint written by older code still
gives the right rows when newer code resumes from it. This module holds what
proves that, against the S5 corpus:

1. **Checkpoint goldens.** The checkpoint the production import writes after
   each growth step: the boundary, the turn count, the hashes, and the
   parser and store carry. Fields that depend on the file system or the wall
   clock are left out.
2. **Old checkpoint, new reader.** Each golden checkpoint is put back beside
   the rows of its step. The next step resumes from it with the current code,
   and the result must equal the next step's store golden. A change to the
   carry, boundary or store semantics that would misread a persisted
   checkpoint fails here, even when the parse goldens do not move.
3. **The epoch manifest** (``goldens/checkpoint-epochs.json``). Per backend,
   the fingerprints of its parse, store and checkpoint goldens, recorded under
   an epoch number. If the current goldens differ from the current epoch's
   fingerprints the test fails. Regenerating the goldens appends a new epoch,
   so a semantic change always shows up in review as an epoch bump. Once
   ``transcript_incremental.PARSER_EPOCH`` exists, it must equal the
   manifest's newest epoch.

A finite corpus cannot represent every record shape, so this guard is
necessary but not sufficient: the periodic full audit stays, and reviewers
decide on an epoch bump for any change to the sources ``_VERSION_SOURCES``
lists today.
"""
from __future__ import annotations

from tests.patching import patch_shared

import hashlib
import json
import os
import pathlib

import pytest

from lib import db, message_store, message_writes
from lib import transcript_incremental as inc

from tests.readmodel import corpus, world
from tests.readmodel.test_readmodel_goldens import GOLDENS, UPDATE, _check

INCREMENTAL = [b for b in corpus.BACKENDS if b in inc.SUPPORTED_BACKENDS]
MANIFEST = GOLDENS / "checkpoint-epochs.json"
# Identity and clock fields: the file system and the run decide them, not the code.
_VOLATILE = ("device", "inode", "mtime_ns", "parser_version", "audited_at", "updated_at",
             "agent_id", "backend_session_id", "source_file")


@pytest.fixture
def importer(monkeypatch):
    clock = world.Clock()
    patch_shared(monkeypatch, message_writes, "now_ms", clock)
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 0)
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    monkeypatch.setattr(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0)
    return clock


def _shadow(monkeypatch, enabled: bool) -> None:
    conf = dict(inc._DEFAULTS, enabled=enabled, sample_every=1,
                max_cpu_seconds_per_hour=1e9, max_load_per_cpu=1e9,
                max_io_pressure=1e9, max_memory_pressure=1e9)
    monkeypatch.setattr(inc, "settings", lambda: dict(conf))
    monkeypatch.setattr(inc, "BUDGET", inc.ShadowBudget())


def _import(backend, transcript) -> str:
    try:
        inc.import_transcript(backend=backend, path=transcript, agent_id=corpus.AGENT,
                              backend_session_id=corpus.SESSION)
    except Exception as exc:  # noqa: BLE001 - pinned like the store goldens
        return f"{type(exc).__name__}: {exc}"
    return "ok"


def _checkpoint(transcript) -> dict | None:
    """The stored row; the carries stay the exact stored JSON (key order is
    part of what a resumed import compares)."""
    row = db.conn().execute(
        "SELECT * FROM transcript_import_checkpoints WHERE agent_id=? AND backend_session_id=?"
        " AND source_file=?", (corpus.AGENT, corpus.SESSION, str(transcript))).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys() if k not in _VOLATILE}


@pytest.mark.parametrize("backend", INCREMENTAL)
def test_the_checkpoint_after_each_growth_step_matches_its_golden(
        tmp_path, backend, importer, monkeypatch):
    _shadow(monkeypatch, True)
    world.prepare(tmp_path / "state.sqlite", backend)
    world.seed(backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    steps = []
    for step in corpus.GROWTH:
        corpus.apply(transcript, backend, step)
        importer.ms += 1000
        steps.append({"step": step, "import": _import(backend, transcript),
                      "checkpoint": _checkpoint(transcript)})
    _check(f"{backend}-checkpoints", {"backend": backend, "steps": steps})


def _restore(transcript, backend, saved: dict) -> None:
    """Put a golden checkpoint back as the file stands now, valid for the
    current code (what an unchanged epoch would promise)."""
    ident = inc.identity(transcript)
    saved = dict(saved)
    saved["parser_carry"] = json.loads(saved.pop("parser_carry_json"))
    saved["store_carry"] = json.loads(saved.pop("store_carry_json"))
    values = dict(saved, agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
                  source_file=str(transcript), parser_version=inc.parser_version(backend),
                  device=ident.device, inode=ident.inode, mtime_ns=ident.mtime_ns,
                  audited_at=0, updated_at=0)
    c = db.conn()
    c.execute("DELETE FROM transcript_import_checkpoints")
    inc.write_checkpoint(c, inc.Checkpoint(**values))
    c.commit()


@pytest.mark.parametrize("backend", INCREMENTAL)
def test_a_stored_checkpoint_resumes_on_the_current_code_to_the_next_golden(
        tmp_path, backend, importer, monkeypatch):
    _shadow(monkeypatch, False)
    checkpoints = json.loads((GOLDENS / f"{backend}-checkpoints.json").read_text())["steps"]
    store = json.loads((GOLDENS / f"{backend}-store.json").read_text())["steps"]
    resumed = 0
    for k in range(1, len(corpus.GROWTH)):
        saved = checkpoints[k - 1]["checkpoint"]
        if saved is None:
            continue
        root = tmp_path / f"step{k}"
        root.mkdir()
        world.prepare(root / "state.sqlite", backend)
        world.seed(backend, importer)
        transcript = corpus.transcript_path(root, backend)
        for step in corpus.GROWTH[:k]:     # the rows as the earlier steps left them
            corpus.apply(transcript, backend, step)
            importer.ms += 1000
            _import(backend, transcript)
        assert world.snapshot(transcript) == {
            key: v for key, v in store[k - 1].items() if key not in ("step", "import")}
        _restore(transcript, backend, saved)
        corpus.apply(transcript, backend, corpus.GROWTH[k])
        importer.ms += 1000
        cp = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
        ident = inc.identity(transcript)
        decision = inc.decide(cp, backend, transcript, ident, now_ms=0, audit_every=10**9,
                              audit_seconds=1e12, max_tail_bytes=1 << 30)
        if decision:
            continue                        # a full import: nothing read from the checkpoint
        message_writes.store_transcript_tail(
            agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
            source_file=str(transcript),
            turns=inc.parse_range(backend, transcript, cp.boundary_offset, ident.size),
            start_seq=cp.boundary_seq, carry=cp.store_carry)
        expected = {key: v for key, v in store[k].items() if key not in ("step", "import")}
        got = world.snapshot(transcript)
        assert got == expected, (
            f"{backend}: resuming {corpus.GROWTH[k]} from the {corpus.GROWTH[k - 1]} "
            f"checkpoint: {_row_diff(got, expected)}")
        resumed += 1
    assert resumed >= 3, "the corpus no longer exercises a resumed import"


def _row_diff(got: dict, expected: dict) -> list:
    """(message id, column, got, expected) per differing cell (synthetic text)."""
    by_id = {m["message_id"]: m for m in expected["messages"]}
    out = []
    for m in got["messages"]:
        e = by_id.pop(m["message_id"], None)
        if e is None:
            out.append((m["message_id"], "extra"))
            continue
        out += [(m["message_id"], k, m[k], e[k]) for k in m if m[k] != e[k]]
    return out + [(i, "missing") for i in by_id]


def _fingerprints(backend: str) -> dict[str, str]:
    out = {}
    for kind in ("parse", "store", "checkpoints"):
        path = GOLDENS / f"{backend}-{kind}.json"
        if path.exists():
            canonical = json.dumps(json.loads(path.read_text()), sort_keys=True,
                                   separators=(",", ":"))
            out[kind] = hashlib.sha256(canonical.encode()).hexdigest()
    return out


def test_every_golden_change_is_recorded_under_a_new_epoch():
    """Run last in a regeneration (it reads the golden files the other
    tests write). In update mode a changed fingerprint appends an epoch."""
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    stale = []
    for backend in corpus.BACKENDS:
        epochs = manifest.setdefault(backend, {})
        current = _fingerprints(backend)
        newest = max(epochs, key=int) if epochs else None
        if newest is not None and epochs[newest] == current:
            continue
        if UPDATE:
            epochs[str(int(newest) + 1 if newest else 1)] = current
        else:
            stale.append(backend)
    if UPDATE:
        MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    assert not stale, (f"goldens changed without a new checkpoint epoch for {stale}: "
                       "regenerate (this appends one) and bump PARSER_EPOCH to match")
    constant = getattr(inc, "PARSER_EPOCH", None)
    if constant is not None:
        for backend in INCREMENTAL:
            assert int(constant[backend]) == int(max(manifest[backend], key=int)), backend


def test_the_update_switch_never_runs_in_ci():
    assert not (UPDATE and os.environ.get("CI")), "goldens are regenerated by hand only"

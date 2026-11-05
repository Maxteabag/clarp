"""RM-N1: a transcript that loses an earlier record (the corpus 'rotate' step:
every later turn shifts down one slot) is stored exactly as a fresh import of
the same transcript would store it: same ids, slots and content."""
from __future__ import annotations

import pytest

from tests.readmodel import corpus, world
from tests.readmodel.test_readmodel_goldens import _import, importer  # noqa: F401


def _rows(transcript):
    return sorted(
        (m["message_id"], m["seq"], m["source_file"], m["role"], m["text"],
         m["tools_json"], m["origin"], m["sender_agent_id"], m["trace_id"])
        for m in world.snapshot(transcript)["messages"])


@pytest.mark.parametrize("backend", corpus.BACKENDS)
def test_a_shifted_transcript_is_stored_as_a_fresh_import_stores_it(tmp_path, backend, importer):
    world.prepare(tmp_path / "grown.sqlite", backend)
    world.seed(backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    for step in corpus.GROWTH:
        corpus.apply(transcript, backend, step)
        importer.ms += 1000
        _import(backend, transcript)
    shifted = _rows(transcript)

    world.prepare(tmp_path / "fresh.sqlite", backend)
    world.seed(backend, importer)
    importer.ms += 1000
    _import(backend, transcript)

    assert shifted == _rows(transcript)

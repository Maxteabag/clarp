"""Voice catalogue + availability annotation."""

from __future__ import annotations

from lib.voices import VOICE_CATALOG, voices_with_availability


def test_catalog_has_no_duplicate_ids():
    """The original VOICE_CATALOG had Brian listed twice. Pin that the lib
    version is dedupable by id when we annotate availability."""
    ids = [v["id"] for v in VOICE_CATALOG]
    # Even if catalogue has dupes, voices_with_availability must dedupe.
    annotated = voices_with_availability({}, for_session="")
    assert len(annotated) == len(set(v["id"] for v in annotated))


def test_taken_by_marks_assigned_voices():
    agents = {
        "claude": {"name": "Mike",   "voice_id": "nPczCjzI2devNBz1zQrb"},
        "rachel": {"name": "Rachel", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    }
    voices = voices_with_availability(agents, for_session="")
    by_id = {v["id"]: v for v in voices}
    assert by_id["nPczCjzI2devNBz1zQrb"]["taken_by"] == "Mike"
    assert by_id["21m00Tcm4TlvDq8ikWAM"]["taken_by"] == "Rachel"
    # An unassigned voice is free.
    assert by_id["AZnzlk1XvdvUeBnXmlld"]["taken_by"] is None


def test_editing_session_sees_own_voice_as_free():
    agents = {
        "rachel": {"name": "Rachel", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    }
    voices = voices_with_availability(agents, for_session="rachel")
    by_id = {v["id"]: v for v in voices}
    # Rachel's current voice is not 'taken' from her POV.
    assert by_id["21m00Tcm4TlvDq8ikWAM"]["taken_by"] is None

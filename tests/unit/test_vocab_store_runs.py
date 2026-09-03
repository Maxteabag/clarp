"""The audit row is completed after transcription and feeds the next compile."""
from __future__ import annotations

from lib import vocab_store
from lib.vocab_budget import CompileResult


def _run(session: str, transcript: str = "") -> int:
    result = CompileResult(payload="Clarp", used=1, capacity=223)
    run_id = vocab_store.record_run(
        result, provider="faster-whisper", model="small.en", session=session,
        trace_id="t")
    if transcript:
        vocab_store.update_run_result(run_id, transcript=transcript, latency_ms=0)
    return run_id


def test_update_attaches_transcript_and_latency_to_the_run():
    run_id = _run("rachel")
    assert vocab_store.update_run_result(run_id, transcript="hello Clarp",
                                         latency_ms=812)
    row = vocab_store.recent_runs(1, session="rachel")[0]
    assert row["transcript"] == "hello Clarp"
    assert vocab_store.run_for_trace("t")["transcript"] == "hello Clarp"
    assert not vocab_store.update_run_result(0, transcript="x", latency_ms=1)
    assert not vocab_store.update_run_result(999999, transcript="x", latency_ms=1)


def test_recent_transcripts_are_per_session_newest_first_and_skip_empty():
    _run("rachel", "first thing")
    _run("mike", "other agent")
    _run("rachel")                      # never answered - no transcript
    _run("rachel", "second thing")
    assert vocab_store.recent_transcripts("rachel") == ("second thing", "first thing")
    assert vocab_store.recent_transcripts("rachel", limit=1) == ("second thing",)
    assert vocab_store.recent_transcripts("") == ()


def test_corrections_come_from_terms_with_recorded_mishearings():
    pack = vocab_store.create_pack("people")
    vocab_store.add_term(pack, "Clarp", often_heard_as="Flarp, Clark")
    vocab_store.add_term(pack, "Knut Thomas", often_heard_as="Newt Thomas")
    vocab_store.add_term(pack, "Oscar")   # nothing recorded
    off = vocab_store.create_pack("disabled", enabled=False)
    vocab_store.add_term(off, "Ghost", often_heard_as="Toast")
    pairs = set(vocab_store.corrections())
    assert pairs == {("Flarp", "Clarp"), ("Clark", "Clarp"),
                     ("Newt Thomas", "Knut Thomas")}

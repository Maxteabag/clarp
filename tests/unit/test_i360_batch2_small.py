"""Issue 360, batch 2 (rows a user can hit): R10, R13, R15."""
from __future__ import annotations

from lib import agents as agents_db
from lib import artifacts


def _agent(tmp_path):
    return agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")


# R15: discarding a pinned artifact answered pinned: true beside pinned_at: null.
def test_r15_a_discarded_pinned_artifact_answers_not_pinned(tmp_path):
    _agent(tmp_path)
    created = artifacts.create(session="mike", type="document", title="Doc", payload={"content": "a"})
    pinned, _ = artifacts.pin(created["artifact_id"], pinned=True)
    assert pinned["pinned"] is True
    gone, changed = artifacts.discard(created["artifact_id"], expected_updated_at=pinned["updated_at"])
    assert changed
    assert gone["pinned_at"] is None
    assert gone["pinned"] is False


# R10: the notification preview of a spoken reply kept bracketed emotion tags.
def test_r10_the_spoken_preview_hides_emotion_tags():
    from lib.user_notifications import _spoken_preview
    preview = _spoken_preview("Done. <speak>[sigh] That took a while. [laughing] All set.</speak>")
    assert "[sigh]" not in preview and "[laughing]" not in preview
    assert preview == "That took a while. All set."


# R13: a session whose transcript is not there yet stayed modelless for up to a minute,
# because the "no transcript" answer was cached per clock minute.
def test_r13_a_missing_transcript_is_asked_again_not_cached(monkeypatch, tmp_path):
    from lib import backends, session_models
    asked = []
    found = tmp_path / "t.jsonl"
    found.write_text("")
    answers = [None, found]

    def model_transcript(session):
        asked.append(session)
        return answers[min(len(asked) - 1, 1)]
    monkeypatch.setattr(backends.by_id("claude"), "model_transcript", model_transcript)
    monkeypatch.setattr(session_models, "_read_model", lambda path, stamp, size: "the-model")
    session = "r13-session"
    assert session_models.transcript_model("claude", session) == ""
    assert session_models.transcript_model("claude", session) == "the-model"   # same minute
    assert len(asked) == 2

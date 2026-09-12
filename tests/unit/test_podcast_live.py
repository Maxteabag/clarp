import base64
import json
import threading
from types import SimpleNamespace

import pytest

from lib import podcast_live


def episode():
    return {"revision": "a" * 64, "transcript": [
        {"start": 0, "end": 10, "text": "Opening"},
        {"start": 80, "end": 90, "text": "Prediction hides delay."},
        {"start": 90, "end": 100, "text": "Next sentence"}],
        "chapters": [{"start": 0, "end": 100, "title": "Prediction",
                      "source": "The server remains authoritative."}],
        "corrections": "A kernel benchmark is not gameplay proof.",
        "notebook_url": "https://notebooklm.google.com/notebook/123"}


def test_context_uses_heard_window_and_source_authority():
    context = podcast_live.context_for(episode(), 89, 100)
    assert "Prediction hides delay." in context
    assert "Opening" not in context
    assert "Next sentence" in context
    assert "server remains authoritative" in context
    assert "not gameplay proof" in context
    cfg = podcast_live.session_config(context)
    assert cfg["model"] == "gpt-live-1"
    assert "delegate_to_agent" not in json.dumps(cfg)
    assert cfg["input"][0]["role"] == "user"


@pytest.mark.parametrize("change", [
    {"revision": ""}, {"transcript": [{"start": 8, "end": 2, "text": "bad"}]},
    {"notebook_url": "javascript:alert(1)"}, {"chapters": []},
    {"transcript": [{"start": float("nan"), "end": 2, "text": "bad"}]},
])
def test_rejects_invalid_episode(change):
    with pytest.raises(ValueError):
        podcast_live.validate_episode({**episode(), **change})


@pytest.mark.parametrize("position", [-1, float("nan"), float("inf"), 101])
def test_rejects_untrusted_playhead(position):
    with pytest.raises(ValueError):
        podcast_live.context_for(episode(), position, 100)


def test_images_do_not_block_voice_and_late_results_are_discarded():
    release, entered = threading.Event(), threading.Event()
    def generate(**kwargs):
        entered.set()
        release.wait(2)
        return base64.b64encode(b"image").decode()
    sent, down, clock = [], [], [0.0]
    c = podcast_live.PodcastConversation(SimpleNamespace(send=sent.append), down.append,
        "unused", "source", images=True, clock=lambda: clock[0], generate=generate)
    try:
        c.receive({"type": "session.input_transcript.delta", "delta": "Explain prediction", "end_ms": 100})
        clock[0] = 3
        c.tick()
        assert entered.wait(1)
        c.receive({"type": "session.output_transcript.delta", "delta": "It hides latency."})
        assert down[-1]["delta"] == "It hides latency."
        c.stop.set()
        release.set()
        c.pool.shutdown(wait=True)
        assert not any(e["type"] == "podcast.image" for e in down)
    finally:
        release.set()
        c.pool.shutdown(wait=True)


def test_podcast_cannot_dispatch_agent_work():
    sent, down = [], []
    c = podcast_live.PodcastConversation(SimpleNamespace(send=sent.append), down.append,
        "unused", "source", images=False)
    try:
        c.receive({"type": "session.delegation.created", "delegation": {"id": "x"}})
        assert not c.tools.delegations
        assert "no access" in sent[-1]
    finally:
        c.pool.shutdown(wait=True)


def test_inaccurate_images_are_reviewed_and_never_returned(monkeypatch):
    import io
    calls = []
    encoded = base64.b64encode(b"\xff\xd8\xfffixture").decode()
    def request(*args, **kwargs):
        calls.append(args[0])
        return io.BytesIO(json.dumps({"data": [{"b64_json": encoded}]}).encode())
    monkeypatch.setattr(podcast_live, "urlopen", request)
    with pytest.raises(ValueError, match="accuracy review"):
        podcast_live.generate_image(api_key="fixture", context="source", question="Explain",
            review=lambda **kwargs: {"approved": False, "reason": "Contradictory timeline"})
    assert len(calls) == 2
    assert "Contradictory timeline" in json.loads(calls[-1].data)["prompt"]


def test_cancelled_diagram_does_not_start_a_paid_retry(monkeypatch):
    import io
    active = [True]
    calls = []
    def request(*args, **kwargs):
        calls.append(args[0])
        return io.BytesIO(json.dumps({"data": [{"b64_json": base64.b64encode(b"\xff\xd8\xfffixture").decode()}]}).encode())
    def review(**kwargs):
        active[0] = False
        return {"approved": False, "reason": "wrong"}
    monkeypatch.setattr(podcast_live, "urlopen", request)
    with pytest.raises(ValueError, match="cancelled"):
        podcast_live.generate_image(api_key="fixture", context="source", question="Explain", review=review,
                                   should_continue=lambda: active[0])
    assert len(calls) == 1

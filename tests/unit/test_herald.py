"""Tests for HeraldManager — server-side raise-your-hand / permission flow.

When a non-current, non-addressed agent emits TTS audio, we don't just dump
it into the queue. The manager broadcasts a tiny "X here, ready for an
update" herald, holds the real audio in a per-session buffer, and releases
that buffer only when:
  * the user grants permission via affirmative + name in /transcribe, or
  * the user switches focus to that agent's pane.

These tests pin the rules. The implementation lives in lib/herald.py.
"""
import sys
import pathlib
import threading
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import herald as herald_module  # noqa: E402
from lib.herald import HeraldManager, HeraldSettings  # noqa: E402


class FakeStream:
    """Minimal stand-in for AudioStream — captures what gets broadcast."""
    def __init__(self):
        self.events: list[dict] = []
    def broadcast(self, event: dict) -> None:
        self.events.append(event)
    @property
    def audio_urls(self) -> list[str]:
        return [e["url"] for e in self.events if e.get("type") == "audio"]


class FailingStream(FakeStream):
    def broadcast(self, event: dict) -> None:
        raise RuntimeError("stream unavailable")


class SelectiveFailStream(FakeStream):
    def __init__(self):
        super().__init__()
        self.fail_urls: set[str] = set()

    def broadcast(self, event: dict) -> None:
        if event.get("url") in self.fail_urls:
            raise RuntimeError("stream unavailable")
        super().broadcast(event)


class FakeTTS:
    """Pretends to synthesize: returns a deterministic url per (text, voice)."""
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
    def synthesize(self, text: str, voice_id: str, session: str | None = None) -> str:
        self.calls.append((text, voice_id, session or ""))
        # Mimic the real engine's filename shape so the scheduler treats
        # heralds the same as any other clip.
        idx = len(self.calls)
        suffix = f"__{session}" if session else ""
        return f"/audio/herald-{idx}{suffix}.mp3"

    def synthesize_herald(
        self, text: str, voice_id: str, session: str | None = None,
    ) -> str:
        return self.synthesize(text, voice_id, session=session)


def make_manager(*, agents=None, herald_settings=None):
    stream = FakeStream()
    tts = FakeTTS()
    agents = agents or {
        "claude": {"name": "Mike",   "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
        "bella":  {"name": "Bella",  "voice_id": "V_BELLA"},
    }
    herald_settings = herald_settings or HeraldSettings()
    hm = HeraldManager(
        stream=stream,
        tts=tts,
        agents=lambda: agents,
        settings=lambda: herald_settings,
    )
    return hm, stream, tts


# ---- baseline forwarding ----

def test_focused_session_clip_broadcasts_directly():
    hm, stream, tts = make_manager()
    hm.set_focus("rachel")
    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    assert result.broadcast is True
    assert result.herald_emitted is False
    assert stream.audio_urls == ["/audio/r1.mp3"]
    assert tts.calls == []


def test_focus_is_read_live_not_cached():
    """Regression (the Bella bug): the herald must read focus LIVE from the
    single source, never cache its own copy. A cached focus drifted from the
    real (DB) focus and made the focused agent herald itself. Here focus changes
    in the injected source with NO set_focus call — the decision must follow it."""
    stream = FakeStream()
    tts = FakeTTS()
    agents = {
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
        "bella":  {"name": "Bella",  "voice_id": "V_BELLA"},
    }
    live_focus = {"sid": "rachel"}
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents,
                       focus_session=lambda: live_focus["sid"])
    # Focus is Rachel → an off-focus Bella clip heralds.
    r1 = hm.ingest_clip("bella", url="/audio/b1.mp3", ts=1)
    assert r1.broadcast is False and r1.herald_emitted is True
    # Focus moves to Bella in the source (no set_focus call) → Bella now plays
    # straight through instead of heralding herself.
    live_focus["sid"] = "bella"
    r2 = hm.ingest_clip("bella", url="/audio/b2.mp3", ts=2)
    assert r2.broadcast is True and r2.herald_emitted is False


def test_awaiting_session_clip_broadcasts_directly():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.set_awaiting("rachel")
    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    assert result.broadcast is True
    assert stream.audio_urls == ["/audio/r1.mp3"]


# ---- regression: streamable flag must survive the herald path ----
#
# Bug reproduced live on 2026-05-25: the client always took the static
# <audio src="/audio/X.mp3"> branch even though the clip was streamable.
# herald._broadcast_audio built its own event from `meta` and only forwarded
# clip_id + trace_id, dropping `streamable` and its delivery URLs, so the
# whole streaming producer chain was wasted.

def test_focused_clip_forwards_streamable_flag_from_meta():
    hm, stream, _ = make_manager()
    hm.set_focus("rachel")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10,
                   meta={"streamable": True, "trace_id": "trace-abc",
                         "stream_url": "/clips/7/stream"})
    audio_events = [e for e in stream.events if e.get("type") == "audio"]
    assert len(audio_events) == 1
    ev = audio_events[0]
    assert ev.get("streamable") is True, (
        f"streamable=true got stripped by herald._broadcast_audio. "
        f"event payload was: {ev}"
    )
    assert ev.get("stream_url") == "/clips/7/stream"
    # trace_id should still come through too (we already forward it).
    assert ev.get("trace_id") == "trace-abc"


def test_non_streamable_clip_does_not_get_stream_url():
    """Inverse: when meta has no streamable flag (e.g. local-mode clips
    or older sidecars), don't add stream_url either — those clients can't
    open a WS."""
    hm, stream, _ = make_manager()
    hm.set_focus("rachel")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10,
                   meta={"trace_id": "trace-abc"})
    ev = next(e for e in stream.events if e.get("type") == "audio")
    assert "streamable" not in ev
    assert "stream_url" not in ev


# ---- herald path ----

def test_background_clip_emits_herald_and_holds_original():
    hm, stream, tts = make_manager()
    hm.set_focus("claude")
    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    assert result.broadcast is False
    assert result.herald_emitted is True
    # One TTS call for the herald with Rachel's voice.
    assert len(tts.calls) == 1
    text, voice, sess = tts.calls[0]
    assert "Rachel" in text
    assert voice == "V_RACHEL"
    assert sess == "rachel"
    # The herald mp3 went to the audio stream; the original did NOT yet.
    assert any("herald" in u for u in stream.audio_urls)
    assert "/audio/r1.mp3" not in stream.audio_urls
    # State: Rachel is pending.
    assert "rachel" in hm.pending_heralds()
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]


def test_short_background_clip_broadcasts_without_herald():
    hm, stream, tts = make_manager(
        herald_settings=HeraldSettings(disabled=False, speak_if_short_chars=120)
    )
    hm.set_focus("claude")
    result = hm.ingest_clip(
        "rachel",
        url="/audio/r1.mp3",
        ts=10,
        meta={"text_len": 23},
    )
    assert result.broadcast is True
    assert result.herald_emitted is False
    assert stream.audio_urls == ["/audio/r1.mp3"]
    assert tts.calls == []
    assert "rachel" not in hm.pending_heralds()
    assert hm.held_clips("rachel") == []


def test_short_bypass_queues_behind_existing_permission_gated_reply():
    hm, stream, _tts = make_manager(
        herald_settings=HeraldSettings(disabled=False, speak_if_short_chars=120)
    )
    hm.set_focus("claude")
    hm.ingest_clip(
        "rachel", url="/audio/long.mp3", ts=1, meta={"text_len": 121})

    short = hm.ingest_clip(
        "rachel", url="/audio/short.mp3", ts=2, meta={"text_len": 20})

    assert short.broadcast is False
    assert hm.held_clips("rachel") == [
        "/audio/long.mp3", "/audio/short.mp3"]
    assert "/audio/long.mp3" not in stream.audio_urls
    assert "/audio/short.mp3" not in stream.audio_urls

    hm.on_user_text("Sure, Rachel, what is it?")
    delivered = [url for url in stream.audio_urls
                 if url in {"/audio/long.mp3", "/audio/short.mp3"}]
    assert delivered == ["/audio/long.mp3", "/audio/short.mp3"]


def test_short_reply_bypass_can_be_disabled_without_changing_threshold():
    hm, stream, tts = make_manager(
        herald_settings=HeraldSettings(
            disabled=False,
            speak_if_short_chars=120,
            short_reply_bypass_enabled=False,
        )
    )
    hm.set_focus("claude")
    result = hm.ingest_clip(
        "rachel", url="/audio/r1.mp3", ts=10, meta={"text_len": 23})

    assert result.broadcast is False
    assert result.herald_emitted is True
    assert len(tts.calls) == 1
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]


def test_missing_short_reply_bypass_setting_migrates_enabled():
    settings = herald_module.get_settings()
    assert settings.short_reply_bypass_enabled is True
    assert settings.as_dict()["short_reply_bypass_enabled"] is True

    herald_module.update_settings({"short_reply_bypass_enabled": False})
    herald_module.update_settings({"speak_if_short_chars": 42})
    migrated = herald_module.get_settings()
    assert migrated.short_reply_bypass_enabled is False
    assert migrated.speak_if_short_chars == 42


@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}])
def test_short_reply_bypass_setting_rejects_non_boolean(value):
    with pytest.raises(ValueError, match="must be a boolean"):
        herald_module.update_settings({"short_reply_bypass_enabled": value})


@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}])
def test_disabled_setting_rejects_non_boolean(value):
    with pytest.raises(ValueError, match="disabled must be a boolean"):
        herald_module.update_settings({"disabled": value})


@pytest.mark.parametrize("value", [None, "120", True, 1.5, [], {}])
def test_short_threshold_rejects_non_integer(value):
    with pytest.raises(ValueError, match="must be an integer"):
        herald_module.update_settings({"speak_if_short_chars": value})


@pytest.mark.parametrize("value", [-1, herald_module.MAX_SPEAK_IF_SHORT_CHARS + 1])
def test_short_threshold_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="must be between"):
        herald_module.update_settings({"speak_if_short_chars": value})


def test_herald_settings_rejects_non_object_payload():
    with pytest.raises(ValueError, match="must be an object"):
        herald_module.update_settings([])


def test_invalid_bypass_payload_does_not_partially_update_other_settings():
    before = herald_module.get_settings()
    with pytest.raises(ValueError, match="must be a boolean"):
        herald_module.update_settings({
            "disabled": not before.disabled,
            "speak_if_short_chars": before.speak_if_short_chars + 10,
            "short_reply_bypass_enabled": "false",
        })
    assert herald_module.get_settings() == before


def test_decision_log_records_final_branch_reason(monkeypatch):
    decisions: list[str] = []

    def capture(event: str, detail: str) -> None:
        if event == "heraldDecision":
            decisions.append(detail)

    monkeypatch.setattr(herald_module, "log", capture)

    focused, _, _ = make_manager()
    focused.set_focus("rachel")
    focused.ingest_clip("rachel", url="/audio/focused.mp3", ts=1)

    awaited, _, _ = make_manager()
    awaited.set_focus("claude")
    awaited.set_awaiting("rachel")
    awaited.ingest_clip("rachel", url="/audio/awaited.mp3", ts=2)

    disabled, _, _ = make_manager(
        herald_settings=HeraldSettings(disabled=True))
    disabled.set_focus("claude")
    disabled.ingest_clip("rachel", url="/audio/disabled.mp3", ts=3)

    short, _, _ = make_manager()
    short.set_focus("claude")
    short.ingest_clip(
        "rachel", url="/audio/short.mp3", ts=4, meta={"text_len": 23})

    unknown, _, _ = make_manager(agents={
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
    })
    unknown.set_focus("claude")
    unknown.ingest_clip("ghost", url="/audio/ghost.mp3", ts=5)

    held, _, _ = make_manager()
    held.set_focus("claude")
    held.ingest_clip("rachel", url="/audio/held-1.mp3", ts=6)
    held.ingest_clip("rachel", url="/audio/held-2.mp3", ts=7)

    no_session, _, _ = make_manager()
    no_session.ingest_clip("", url="/audio/system.mp3", ts=8)

    reasons = {
        part.split("=", 1)[1]
        for detail in decisions
        for part in detail.split()
        if part.startswith("reason=")
    }
    assert reasons == {
        "focused_session", "awaited_session", "herald_disabled",
        "short_reply_bypass", "unknown_session", "herald_emitted",
        "already_pending", "no_session",
    }


def test_decision_log_does_not_claim_failed_broadcast_was_emitted(monkeypatch):
    decisions: list[str] = []
    monkeypatch.setattr(
        herald_module, "log",
        lambda event, detail: decisions.append(detail)
        if event == "heraldDecision" else None,
    )
    stream = FailingStream()
    tts = FakeTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("claude")

    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)

    assert result.broadcast is False
    assert result.herald_emitted is False
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]
    assert "rachel" not in hm.pending_heralds()
    assert any("action=hold" in line
               and "reason=herald_broadcast_failed" in line
               for line in decisions)


def test_failed_herald_is_retryable_and_only_success_becomes_pending():
    stream = SelectiveFailStream()
    tts = FakeTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("claude")
    stream.fail_urls.add("/audio/herald-1__rachel.mp3")

    first = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    assert first.herald_emitted is False
    assert "rachel" not in hm.pending_heralds()
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]

    stream.fail_urls.clear()
    second = hm.ingest_clip("rachel", url="/audio/r2.mp3", ts=2)
    assert second.herald_emitted is True
    assert "rachel" in hm.pending_heralds()
    assert hm.held_clips("rachel") == ["/audio/r1.mp3", "/audio/r2.mp3"]
    assert len(tts.calls) == 2


def test_failed_herald_keeps_permission_gate_against_short_bypass():
    stream = SelectiveFailStream()
    tts = FakeTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("claude")
    stream.fail_urls.add("/audio/herald-1__rachel.mp3")
    hm.ingest_clip(
        "rachel", url="/audio/long.mp3", ts=1, meta={"text_len": 121})

    short = hm.ingest_clip(
        "rachel", url="/audio/short.mp3", ts=2, meta={"text_len": 20})

    assert short.broadcast is False
    assert "rachel" not in hm.pending_heralds()
    assert hm.held_clips("rachel") == [
        "/audio/long.mp3", "/audio/short.mp3"]
    assert "/audio/long.mp3" not in stream.audio_urls
    assert "/audio/short.mp3" not in stream.audio_urls

    stream.fail_urls.clear()
    hm.set_focus("rachel")
    delivered = [url for url in stream.audio_urls
                 if url in {"/audio/long.mp3", "/audio/short.mp3"}]
    assert delivered == ["/audio/long.mp3", "/audio/short.mp3"]


def test_focus_change_during_first_herald_cannot_miss_buffer_flush():
    synth_started = threading.Event()
    allow_synth = threading.Event()

    class BlockingTTS(FakeTTS):
        def synthesize(self, text, voice_id, session=None):
            synth_started.set()
            assert allow_synth.wait(timeout=1)
            return super().synthesize(text, voice_id, session)

    stream = FakeStream()
    tts = BlockingTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    live_focus = {"sid": "claude"}
    focus_lock = threading.RLock()
    hm = HeraldManager(
        stream=stream, tts=tts, agents=lambda: agents,
        focus_session=lambda: live_focus["sid"],
        focus_guard=lambda: focus_lock)
    hm.set_focus("claude")

    ingest = threading.Thread(
        target=lambda: hm.ingest_clip(
            "rachel", url="/audio/r1.mp3", ts=1))
    def change_focus():
        with focus_lock:
            live_focus["sid"] = "rachel"
            hm.set_focus("rachel")

    focus = threading.Thread(target=change_focus)
    ingest.start()
    assert synth_started.wait(timeout=1)
    focus.start()
    focus.join(timeout=0.5)
    assert not focus.is_alive(), "focus must not wait for provider TTS"
    allow_synth.set()
    ingest.join(timeout=1)

    assert not ingest.is_alive()
    assert hm.held_clips("rachel") == []
    assert "rachel" not in hm.pending_heralds()
    assert "/audio/r1.mp3" in stream.audio_urls
    assert not any("herald" in url for url in stream.audio_urls)


def test_superseded_herald_reports_failed_flush_as_retained(monkeypatch):
    synth_started = threading.Event()
    allow_synth = threading.Event()
    decisions: list[str] = []
    monkeypatch.setattr(
        herald_module, "log",
        lambda event, detail: decisions.append(detail)
        if event == "heraldDecision" else None,
    )

    class BlockingTTS(FakeTTS):
        def synthesize_herald(self, text, voice_id, session=None):
            synth_started.set()
            assert allow_synth.wait(timeout=1)
            return super().synthesize_herald(text, voice_id, session)

    stream = SelectiveFailStream()
    stream.fail_urls.add("/audio/r1.mp3")
    live_focus = {"sid": "claude"}
    focus_lock = threading.RLock()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(
        stream=stream, tts=BlockingTTS(), agents=lambda: agents,
        focus_session=lambda: live_focus["sid"],
        focus_guard=lambda: focus_lock)
    results: list = []
    ingest = threading.Thread(target=lambda: results.append(
        hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)))
    ingest.start()
    assert synth_started.wait(timeout=1)
    with focus_lock:
        live_focus["sid"] = "rachel"
        hm.set_focus("rachel")
    allow_synth.set()
    ingest.join(timeout=1)

    assert results[0].broadcast is False
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]
    assert any("action=hold" in line
               and "reason=superseded_flush_failed" in line
               for line in decisions)


def test_failed_direct_broadcast_retries_in_order_with_metadata():
    stream = SelectiveFailStream()
    tts = FakeTTS()
    agents = {
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("rachel")
    stream.fail_urls.add("/audio/r1.mp3")

    first = hm.ingest_clip(
        "rachel", url="/audio/r1.mp3", ts=1,
        meta={"clip_id": "clip-1"})
    assert first.broadcast is False
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]

    stream.fail_urls.clear()
    second = hm.ingest_clip(
        "rachel", url="/audio/r2.mp3", ts=2,
        meta={"clip_id": "clip-2"})

    assert second.broadcast is True
    assert hm.held_clips("rachel") == []
    delivered = [event for event in stream.events
                 if event.get("url") in {"/audio/r1.mp3", "/audio/r2.mp3"}]
    assert [event["url"] for event in delivered] == [
        "/audio/r1.mp3", "/audio/r2.mp3"]
    assert [event["clip_id"] for event in delivered] == ["clip-1", "clip-2"]
    assert not any(event.get("herald") for event in delivered)


def test_synthesis_failure_has_distinct_retryable_reason(monkeypatch):
    decisions: list[str] = []
    monkeypatch.setattr(
        herald_module, "log",
        lambda event, detail: decisions.append(detail)
        if event == "heraldDecision" else None,
    )

    class FailingTTS:
        def synthesize_herald(self, *_args, **_kwargs):
            raise RuntimeError("synthesis unavailable")

    stream = FakeStream()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=FailingTTS(), agents=lambda: agents)
    hm.set_focus("claude")

    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)

    assert result.herald_emitted is False
    assert "rachel" not in hm.pending_heralds()
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]
    assert any("reason=synthesis_failed" in line for line in decisions)


def test_long_background_clip_still_heralds_when_enabled():
    hm, stream, tts = make_manager(
        herald_settings=HeraldSettings(disabled=False, speak_if_short_chars=120)
    )
    hm.set_focus("claude")
    result = hm.ingest_clip(
        "rachel",
        url="/audio/r1.mp3",
        ts=10,
        meta={"text_len": 121},
    )
    assert result.broadcast is False
    assert result.herald_emitted is True
    assert len(tts.calls) == 1
    assert "/audio/r1.mp3" not in stream.audio_urls
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]


def test_disabled_herald_broadcasts_long_background_clip_directly():
    hm, stream, tts = make_manager(
        herald_settings=HeraldSettings(disabled=True, speak_if_short_chars=120)
    )
    hm.set_focus("claude")
    result = hm.ingest_clip(
        "rachel",
        url="/audio/r1.mp3",
        ts=10,
        meta={"text_len": 1000},
    )
    assert result.broadcast is True
    assert result.herald_emitted is False
    assert stream.audio_urls == ["/audio/r1.mp3"]
    assert tts.calls == []
    assert hm.held_clips("rachel") == []


def test_subsequent_clips_from_held_agent_stack_without_reheralding():
    hm, stream, tts = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    hm.ingest_clip("rachel", url="/audio/r2.mp3", ts=11)
    # Still only the single herald.
    assert len(tts.calls) == 1
    assert hm.held_clips("rachel") == ["/audio/r1.mp3", "/audio/r2.mp3"]


# ---- release via intent ----

def test_grant_via_user_text_flushes_held_clips():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    hm.ingest_clip("rachel", url="/audio/r2.mp3", ts=11)
    res = hm.on_user_text("Sure, Rachel, what is it?")
    assert "rachel" in res.granted
    assert "rachel" not in hm.pending_heralds()
    # Both held clips drained into the stream, in order.
    assert stream.audio_urls[-2:] == ["/audio/r1.mp3", "/audio/r2.mp3"]


def test_partial_flush_retains_failed_clip_and_metadata_for_retry(monkeypatch):
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        herald_module, "log", lambda event, detail: logs.append((event, detail)))
    stream = SelectiveFailStream()
    tts = FakeTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("claude")
    hm.ingest_clip(
        "rachel", url="/audio/r1.mp3", ts=1, meta={"clip_id": "clip-1"})
    hm.ingest_clip(
        "rachel", url="/audio/r2.mp3", ts=2, meta={"clip_id": "clip-2"})
    stream.fail_urls.add("/audio/r2.mp3")

    hm.on_user_text("Sure, Rachel, what is it?")

    assert hm.held_clips("rachel") == ["/audio/r2.mp3"]
    assert "rachel" in hm.pending_heralds()
    assert any(event == "heraldFlushPartial" for event, _ in logs)

    stream.fail_urls.clear()
    hm.set_focus("rachel")
    assert hm.held_clips("rachel") == []
    assert "rachel" not in hm.pending_heralds()
    retried = next(event for event in stream.events
                   if event.get("url") == "/audio/r2.mp3")
    assert retried["clip_id"] == "clip-2"


def test_flush_stops_at_first_failure_and_preserves_clip_order():
    stream = SelectiveFailStream()
    tts = FakeTTS()
    agents = {
        "claude": {"name": "Mike", "voice_id": "V_MIKE"},
        "rachel": {"name": "Rachel", "voice_id": "V_RACHEL"},
    }
    hm = HeraldManager(stream=stream, tts=tts, agents=lambda: agents)
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    hm.ingest_clip("rachel", url="/audio/r2.mp3", ts=2)
    stream.fail_urls.add("/audio/r1.mp3")

    hm.on_user_text("Sure, Rachel, what is it?")

    assert hm.held_clips("rachel") == ["/audio/r1.mp3", "/audio/r2.mp3"]
    assert "/audio/r2.mp3" not in stream.audio_urls

    stream.fail_urls.clear()
    hm.set_focus("rachel")
    delivered = [url for url in stream.audio_urls
                 if url in {"/audio/r1.mp3", "/audio/r2.mp3"}]
    assert delivered == ["/audio/r1.mp3", "/audio/r2.mp3"]


def test_decline_keeps_buffer():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    res = hm.on_user_text("Not now, Rachel.")
    assert "rachel" in res.declined
    assert "rachel" in hm.pending_heralds()
    assert hm.held_clips("rachel") == ["/audio/r1.mp3"]
    assert "/audio/r1.mp3" not in stream.audio_urls


def test_mere_mention_does_not_grant():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    res = hm.on_user_text("tell rachel I said hi")
    assert "rachel" not in res.granted
    assert "rachel" in hm.pending_heralds()
    assert "/audio/r1.mp3" not in stream.audio_urls


def test_grant_one_does_not_release_others():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    hm.ingest_clip("bella",  url="/audio/b1.mp3", ts=11)
    hm.on_user_text("go ahead rachel")
    assert "rachel" not in hm.pending_heralds()
    assert "bella"  in     hm.pending_heralds()
    assert "/audio/r1.mp3" in stream.audio_urls
    assert "/audio/b1.mp3" not in stream.audio_urls


# ---- release via focus shift ----

def test_focus_shift_to_held_agent_flushes_their_buffer():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    hm.set_focus("rachel")
    assert "rachel" not in hm.pending_heralds()
    assert "/audio/r1.mp3" in stream.audio_urls


def test_awaiting_shift_to_held_agent_flushes_their_buffer():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    # the user asks Rachel something — now awaiting Rachel; her buffer drains.
    hm.set_awaiting("rachel")
    assert "rachel" not in hm.pending_heralds()
    assert "/audio/r1.mp3" in stream.audio_urls


# ---- robustness ----

def test_focused_agent_heralds_when_awaiting_someone_else():
    """Mike's pane is in focus, but the user just spoke to Rachel. While we
    await Rachel, Mike popping in is an interruption — he must raise his hand."""
    hm, stream, tts = make_manager()
    hm.set_focus("claude")
    hm.set_awaiting("rachel")
    result = hm.ingest_clip("claude", url="/audio/c1.mp3", ts=1)
    assert result.broadcast is False
    assert result.herald_emitted is True
    assert "claude" in hm.pending_heralds()
    assert "/audio/c1.mp3" not in stream.audio_urls


def test_focused_agent_passes_through_once_awaiting_expires(monkeypatch):
    # Custom clock so we can fast-forward past the awaiting TTL.
    t = [1000.0]
    from lib.herald import HeraldManager
    stream = type("S", (), {"events": [], "broadcast": lambda self, e: self.events.append(e)})()
    tts = type("T", (), {
        "synthesize_herald": lambda self, *a, **k: "/audio/h.mp3",
    })()
    hm = HeraldManager(stream=stream, tts=tts,
                       agents=lambda: {"claude": {"name": "Mike", "voice_id": "v"}},
                       awaiting_ttl=10.0, clock=lambda: t[0])
    hm.set_focus("claude")
    hm.set_awaiting("rachel")
    # Within TTL → herald.
    r1 = hm.ingest_clip("claude", url="/audio/c1.mp3", ts=1)
    assert r1.herald_emitted is True
    # After TTL elapses → focus regains right-of-way.
    t[0] += 30
    r2 = hm.ingest_clip("claude", url="/audio/c2.mp3", ts=2)
    assert r2.broadcast is True


def test_unknown_session_passes_through_without_herald():
    # No TTS metadata for some random session → just broadcast the clip.
    hm, stream, tts = make_manager()
    hm.set_focus("claude")
    result = hm.ingest_clip("ghost", url="/audio/g1.mp3", ts=10)
    assert result.broadcast is True
    assert tts.calls == []
    assert "/audio/g1.mp3" in stream.audio_urls


def test_user_text_with_no_pending_heralds_is_a_noop():
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    res = hm.on_user_text("go ahead rachel")
    assert res.granted == []
    assert res.declined == []
    assert stream.audio_urls == []


# ---- regressions from the live-testing session ----

def test_switching_focus_ends_await_on_previous_agent():
    """Ask Rachel, then open Bella: Rachel's reply must raise its hand
    (herald) instead of playing through and interrupting Bella.

    Regression — Domi "told me about Toxoplasma" while I was in Adam's chat."""
    hm, stream, tts = make_manager()
    hm.set_focus("rachel")
    hm.set_awaiting("rachel")     # you asked Rachel
    hm.set_focus("bella")         # ...then navigated to Bella

    result = hm.ingest_clip("rachel", url="/audio/r1.mp3", ts=10)
    assert not result.broadcast, "Rachel should NOT play through after you left her"
    assert result.herald_emitted, "Rachel should raise her hand instead"


def test_herald_announcement_event_is_flagged():
    """The 'X here, ready for an update' announcement is flagged herald=True so
    the client plays it regardless of which agent is focused.

    Regression — badge fired but 'Domi here' never played."""
    hm, stream, tts = make_manager()
    hm.set_focus("rachel")                       # Bella is a background agent
    hm.ingest_clip("bella", url="/audio/b1.mp3", ts=10)

    heralds = [e for e in stream.events
               if e.get("type") == "audio" and e.get("herald")]
    assert len(heralds) == 1, f"expected one flagged herald event, got {stream.events}"
    assert heralds[0]["session"] == "bella"
    # The real reply is held, not broadcast.
    assert "/audio/b1.mp3" not in stream.audio_urls


def test_flushed_held_clips_are_flagged_herald():
    """When held clips are released (you grant / open the agent), they're
    flagged herald=True too — so they play even while you're focused elsewhere
    (the queue would otherwise drop them as stale)."""
    hm, stream, tts = make_manager()
    hm.set_focus("rachel")
    hm.ingest_clip("bella", url="/audio/b1.mp3", ts=10)   # held + announced
    stream.events.clear()

    hm.set_focus("bella")                                  # opening Bella flushes
    flushed = [e for e in stream.events
               if e.get("type") == "audio" and e["url"] == "/audio/b1.mp3"]
    assert len(flushed) == 1
    assert flushed[0].get("herald") is True

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.protocol import TurnSource  # noqa: E402
from lib.timing import HOOK_TIMING  # noqa: E402

_HOOKS = pathlib.Path(__file__).resolve().parents[2] / "plugin" / "hooks"
sys.path.insert(0, str(_HOOKS))
import pwa_source_flag  # noqa: E402


def test_marker_requires_matching_session(tmp_path):
    marker = tmp_path / "rachel"
    marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} claude {time.time():.3f}\n")

    assert pwa_source_flag._read_pwa_marker(marker, "rachel").fresh is False


def test_marker_accepts_matching_fresh_session(tmp_path):
    marker = tmp_path / "rachel"
    marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} rachel {time.time():.3f}\n")

    assert pwa_source_flag._read_pwa_marker(marker, "rachel").fresh is True


def test_marker_rejects_stale_values(tmp_path):
    marker = tmp_path / "rachel"
    old = time.time() - HOOK_TIMING.pwa_source_fresh_window_sec - 1
    marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} rachel {old:.3f}\n")

    assert pwa_source_flag._read_pwa_marker(marker, "rachel").fresh is False


def test_marker_carries_trace_id_for_hook_continuity(tmp_path):
    marker = tmp_path / "rachel"
    marker.write_text(
        f"{TurnSource.PWA_VOICE_MARKER} rachel {time.time():.3f} trace-123\n"
    )

    parsed = pwa_source_flag._read_pwa_marker(marker, "rachel")

    assert parsed.fresh is True
    assert parsed.trace_id == "trace-123"
    assert parsed.synthesize_audio is True


def test_marker_carries_disabled_audio_policy(tmp_path):
    marker = tmp_path / "rachel"
    marker.write_text(
        f"{TurnSource.PWA_VOICE_MARKER} rachel {time.time():.3f} trace-123 0\n"
    )

    parsed = pwa_source_flag._read_pwa_marker(marker, "rachel")

    assert parsed.fresh is True
    assert parsed.trace_id == "trace-123"
    assert parsed.synthesize_audio is False


# The PARAGRAPH_BREAK_SENTINEL constant — and its scrubber in
# claude_transcript — were deleted along with the rest of the pre-clarp
# legacy. Per-paragraph TTS now rides on clarp's native
# --include-partial-messages stream-json deltas.


def test_app_turn_always_forbids_interactive_questions():
    """Any app-dispatched turn gets the no-interactive-questions rule, even
    when it's silent (synthesize_audio off / typed). Spoken turns also get the
    <speak> guidance."""
    silent = pwa_source_flag._build_additional_context(app_dispatched=True, voiced=False)
    assert "AskUserQuestion" in silent
    assert "<speak>" not in silent

    spoken = pwa_source_flag._build_additional_context(app_dispatched=True, voiced=True)
    assert "AskUserQuestion" in spoken
    assert "<speak>" in spoken


def test_spoken_hook_context_requests_conversational_delivery_for_all_speech():
    spoken = pwa_source_flag._build_additional_context(
        app_dispatched=True,
        voiced=True,
    )

    assert "Every spoken response should sound conversational" in spoken
    assert "do not reserve them for uncertainty" in spoken
    assert "When unsure or working through something complex" not in spoken
    assert "few or no fillers" not in spoken


def test_non_app_turn_emits_no_context():
    """A turn the app didn't dispatch (e.g. third-party local terminal) and
    isn't voiced gets no injected context at all."""
    assert pwa_source_flag._build_additional_context(app_dispatched=False, voiced=False) == ""


def _run_hook(monkeypatch, session, claude_uuid):
    import io
    import json
    monkeypatch.setenv("CLAUDE_PWA_SESSION", session)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": claude_uuid, "prompt": "hi"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    pwa_source_flag.main()


def test_claude_fallback_uuid_never_replaces_a_codex_agents_thread(
        tmp_path, monkeypatch):
    """Gordon (Codex) answered through a Claude fallback, and the hook bound
    Claude's UUID over his Codex thread: the chat emptied and the thread was
    no longer resumable."""
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Gordon", voice_id="V", cwd=str(tmp_path), session="gordon",
        backend="codex")
    agents_db.bind_backend_session(agent_id, "codex-thread")

    _run_hook(monkeypatch, "gordon", "claude-fallback-uuid")

    assert agents_db.live_backend_session(agent_id) == "codex-thread"


def test_claude_agent_is_still_bound_by_the_hook(tmp_path, monkeypatch):
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")

    _run_hook(monkeypatch, "rachel", "claude-uuid")

    assert agents_db.live_backend_session(agent_id) == "claude-uuid"

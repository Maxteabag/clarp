import pathlib
import sys
import time

import pytest

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


def test_app_turn_always_carries_the_shared_clarp_skills_guidance():
    """Any app-dispatched Claude turn gets the guidance every provider gets,
    once, even when it's silent (synthesize_audio off / typed). Spoken turns
    also get the <speak> guidance."""
    from lib.clarp_guidance import CLARP_SKILLS_GUIDANCE
    silent = pwa_source_flag._build_additional_context(app_dispatched=True, voiced=False)
    assert silent == CLARP_SKILLS_GUIDANCE

    spoken = pwa_source_flag._build_additional_context(app_dispatched=True, voiced=True)
    assert spoken.count(CLARP_SKILLS_GUIDANCE) == 1
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


def test_spoken_hook_context_follows_the_agents_humanness_and_voice(monkeypatch):
    from lib import config, voice_humanness
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="gemini", tts_fallback="cartesia",
        tts_agent_overrides={"theo": {"provider": "cartesia"}}))
    voice_humanness.update({"default": 10, "agents": {"quiet": 0, "mid": 5}})

    def spoken(name):
        return pwa_source_flag._build_additional_context(
            app_dispatched=True, voiced=True, agent={"name": name, "session": name})

    ten = spoken("rachel")
    assert "Humanness: 10/10." in ten and "frequent natural fillers" in ten
    assert "<vox>[laughing]</vox>" in ten and "Never write pause tags" in ten
    assert "first output" in ten.lower() or "VERY FIRST output" in ten
    assert "[laughing]" not in spoken("theo")             # Cartesia voice
    assert "occasional fillers" in spoken("mid")
    zero = spoken("quiet")
    assert "Humanness: 0/10." in zero and "<vox>" not in zero and "[laughing]" not in zero


def test_voiced_hook_run_emits_the_level_ten_guidance(tmp_path, monkeypatch):
    import json
    from lib import agents as agents_db, voice_humanness
    agents_db.create_agent(persona="Rachel", voice_id="V", cwd=str(tmp_path),
                           session="rachel")
    voice_humanness.update({"agents": {"rachel": 10}})
    marker = pwa_source_flag.PATHS.source_marker("rachel")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} rachel {time.time():.3f}\n")

    _run_hook(monkeypatch, "rachel", "claude-uuid")

    context = json.loads(sys.stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "<speak>" in context and "Humanness: 10/10." in context


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


# --- trace ownership ------------------------------------------------------
#
# Mochi-PR-REVIEW, 2026-10-05: a Clarp-dispatched claude -p turn took trace
# 19f671f72ccfa877 at 16:11Z and was still working at 17:38Z when a
# background-task notification fired UserPromptSubmit inside the same
# process. The 1 h trace TTL had passed, so the hook minted
# local-1a10d256179, replaced the turn's trace with it, and the runtime took
# its own running turn for superseded and leaked the slot.

def _age_trace(agent_id, ms):
    from lib import agents as agents_db
    from lib.db import conn
    conn().execute("UPDATE traces SET updated_at = updated_at - ? WHERE agent_id = ?",
                   (ms, agent_id))


def _stored_trace(agent_id):
    from lib.db import conn
    row = conn().execute("SELECT trace_id FROM traces WHERE agent_id = ?",
                         (agent_id,)).fetchone()
    return row["trace_id"] if row else None


def _turn_traces(agent_id):
    from lib.db import conn
    return [r["trace_id"] for r in conn().execute(
        "SELECT trace_id FROM turns WHERE agent_id = ? ORDER BY turn_id", (agent_id,))]


def _long_turn(tmp_path, state="tool"):
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Mochi", voice_id="V", cwd=str(tmp_path), session="mochi")
    agents_db.set_trace(agent_id, "19f671f72ccfa877")
    agents_db.record_state(agent_id, state)
    _age_trace(agent_id, agents_db.TRACE_TTL_MS + 27 * 60_000)
    return agent_id


def test_a_running_turns_trace_does_not_expire(tmp_path):
    from lib import agents as agents_db
    agent_id = _long_turn(tmp_path)

    assert agents_db.get_trace(agent_id) == "19f671f72ccfa877"


def test_a_settled_agents_trace_still_expires(tmp_path):
    from lib import agents as agents_db
    agent_id = _long_turn(tmp_path, state="done")

    assert agents_db.get_trace(agent_id) is None


def test_hook_inside_a_long_clarp_turn_keeps_the_turns_trace(tmp_path, monkeypatch):
    agent_id = _long_turn(tmp_path)

    _run_hook(monkeypatch, "mochi", "claude-uuid")

    assert _stored_trace(agent_id) == "19f671f72ccfa877"
    assert _turn_traces(agent_id) == ["19f671f72ccfa877"]


def test_hook_in_a_clarp_process_never_replaces_an_expired_trace(tmp_path, monkeypatch):
    # The turn's state already reads settled (or was never recorded), yet the
    # prompt still arrives inside the process Clarp started for that trace.
    # Only the dispatcher hands out traces there.
    agent_id = _long_turn(tmp_path, state="done")

    _run_hook(monkeypatch, "mochi", "claude-uuid")

    assert _stored_trace(agent_id) == "19f671f72ccfa877"
    assert _turn_traces(agent_id) == ["19f671f72ccfa877"]


def test_hook_in_a_clarp_process_with_no_trace_yet_still_opens_a_turn(tmp_path, monkeypatch):
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Mochi", voice_id="V", cwd=str(tmp_path), session="mochi")

    _run_hook(monkeypatch, "mochi", "claude-uuid")

    minted = _stored_trace(agent_id)
    assert minted and minted.startswith("local-")
    assert _turn_traces(agent_id) == [minted]


def test_terminal_prompt_after_an_expired_trace_still_starts_a_new_one(tmp_path, monkeypatch):
    # A local terminal session is not a Clarp turn: once the previous trace
    # has expired and the agent is settled, its next prompt is a new turn.
    import io
    import json
    from lib import agents as agents_db
    agent_id = _long_turn(tmp_path, state="done")
    agents_db.bind_backend_session(agent_id, "terminal-uuid")
    monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "terminal-uuid", "prompt": "hi"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    pwa_source_flag.main()

    fresh = _stored_trace(agent_id)
    assert fresh != "19f671f72ccfa877" and fresh.startswith("local-")
    assert _turn_traces(agent_id) == [fresh]


def test_a_claude_turn_is_told_whose_turn_it_is():
    context = pwa_source_flag._build_additional_context(
        app_dispatched=True, voiced=False, agent={"persona": "Koko", "session": "koko-5850"})
    assert "This turn belongs to Koko (Clarp session koko-5850)" in context


@pytest.mark.parametrize("level,label", [(0, None), (1, "milestones"), (2, "steps"),
                                         (3, "running commentary")])
@pytest.mark.parametrize("delivery", ["voiced", "audio-disabled", "typed"])
def test_hook_context_carries_only_the_speaking_agents_narration_level(
        tmp_path, monkeypatch, level, label, delivery):
    import json
    from lib import agents as agents_db

    speaker = agents_db.create_agent(persona="Speaker", voice_id="V", cwd=str(tmp_path),
                                    session="speaker")
    agents_db.update_agent(speaker, voice_verbosity=level)
    other = agents_db.create_agent(persona="Other", voice_id="V", cwd=str(tmp_path),
                                  session="other")
    agents_db.update_agent(other, voice_verbosity=(level + 1) % 4)
    if delivery != "typed":
        marker = pwa_source_flag.PATHS.source_marker("speaker")
        marker.parent.mkdir(parents=True, exist_ok=True)
        audio = int(delivery == "voiced")
        marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} speaker {time.time():.3f} narration-trace {audio}\n")

    _run_hook(monkeypatch, "speaker", "speaker-uuid")

    context = json.loads(sys.stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
    if delivery == "voiced" and level:
        assert context.count(f"Narration level: {label}.") == 1
        assert context.count("Narration level:") == 1
    else:
        assert "Narration level:" not in context


@pytest.fixture
def narration_hook_pair(tmp_path):
    """Compare complete main/candidate hook executions, not selected strings."""
    import json
    import os
    import subprocess
    from lib import agents as agents_db
    from lib.db import conn

    root = pathlib.Path(__file__).resolve().parents[2]
    agent_id = agents_db.create_agent(persona="Narration test", voice_id="V", cwd=str(tmp_path),
                                     session="narration-test")
    def run_pair(*, stored=0, **case):
        # SQLite permits a REAL infinity in this INTEGER-affinity column.
        # Row projections exercise NULL/missing/text without weakening NOT NULL.
        conn().execute("UPDATE agents SET voice_verbosity=? WHERE agent_id=?", (stored, agent_id))
        case_file = tmp_path / "case.json"
        case_file.write_text(json.dumps(case))
        results = []
        for version in ("main", "candidate"):
            report_file = tmp_path / f"reports-{version}.json"
            result = subprocess.run(
                [sys.executable, str(root / "tests/fixtures/pwa_narration_hook_runner.py"),
                 str(root), str(case_file), version, str(report_file)],
                input=json.dumps({"session_id": "narration-uuid", "prompt": "hi"}).encode(),
                env={**os.environ, "CLAUDE_PWA_SESSION": "narration-test"},
                capture_output=True, timeout=10)
            reports = json.loads(report_file.read_text())
            results.append((result, reports))
        return results
    return run_pair


@pytest.mark.parametrize("level", [0, 2])
def test_narration_import_failure_keeps_main_output_and_reports_privately(narration_hook_pair, level):
    (main, _), (actual, reports) = narration_hook_pair(stored=level, unavailable=True)
    assert main.returncode == actual.returncode == 0
    assert main.stdout == actual.stdout
    assert main.stderr == actual.stderr == b""
    assert reports == [{"source": "userprompt_hook", "event": "narrationFail", "level": "error",
                        "detail": {"error": "ImportError"}}]


def test_narration_infinity_keeps_main_output_and_reports_privately(narration_hook_pair):
    (main, _), (actual, reports) = narration_hook_pair(stored=float("inf"))
    assert main.returncode == actual.returncode == 0
    assert main.stdout == actual.stdout
    assert main.stderr == actual.stderr == b""
    assert reports == [{"source": "userprompt_hook", "event": "narrationFail", "level": "error",
                        "detail": {"error": "OverflowError"}}]


@pytest.mark.parametrize("case", [{"stored": 0}, {"resolved_level": None},
                                  {"resolved_level": "missing"}, {"resolved_level": "garbage"},
                                  {"stored": -1}])
def test_quiet_narration_whole_output_matches_main(narration_hook_pair, case):
    (main, _), (actual, reports) = narration_hook_pair(**case)
    assert main.returncode == actual.returncode == 0
    assert main.stdout == actual.stdout
    assert main.stderr == actual.stderr == b""
    assert reports == []


@pytest.mark.parametrize("case,expected_level", [({"stored": 99}, 3),
                                                ({"resolved_level": "2"}, 2)])
def test_narration_odd_values_follow_shared_catalogue(narration_hook_pair, case, expected_level):
    import json
    from lib import voice_verbosity

    (main, _), (actual, reports) = narration_hook_pair(**case)
    assert main.returncode == actual.returncode == 0
    assert main.stderr == actual.stderr == b""
    assert reports == []
    expected = json.loads(main.stdout)
    context = expected["hookSpecificOutput"]["additionalContext"]
    header = "\n\nNARRATION TEST GROUP HEADER"
    assert context.endswith(header)
    clause = voice_verbosity.narration_clause(expected_level)
    expected["hookSpecificOutput"]["additionalContext"] = context[:-len(header)] + "\n\n" + clause + header
    assert actual.stdout == (json.dumps(expected) + "\n").encode()


def test_narration_failure_writes_one_private_line_with_capture_off(
        narration_hook_pair, tmp_path):
    from lib import diagnostics_settings, telemetry
    from lib.paths import RuntimePaths

    diagnostics_settings.update({"enabled": False, "categories": []})
    (main, _), (actual, _) = narration_hook_pair(stored=2, unavailable=True, real_storage=True)
    assert main.returncode == actual.returncode == 0
    assert actual.stdout == main.stdout
    assert actual.stderr == main.stderr == b""
    lines = RuntimePaths.from_home(tmp_path).hook_log.read_text().splitlines()
    assert len(lines) == 1
    assert "userprompt_hook" in lines[0] and "narrationFail ImportError" in lines[0]
    assert "test: narration unavailable" not in lines[0]
    assert telemetry.conn().execute(
        "SELECT COUNT(*) FROM diagnostic_events WHERE event='narrationFail'").fetchone()[0] == 0
    assert diagnostics_settings.get().enabled is False

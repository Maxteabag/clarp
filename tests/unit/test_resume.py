"""Tests for resume.resume_missing_sessions and helpers.

After the legacy purge: no file-map fallback, no newest-in-cwd fallback.
An agent either has a saved UUID with a matching JSONL on
disk (resumed) or starts fresh on its next /send.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.resume import (  # noqa: E402
    find_session_jsonl,
    resume_missing_sessions,
    _cwd_from_project_dir,
)


def _make_jsonl(projects: pathlib.Path, cwd: str, sid: str, mtime: float | None = None):
    encoded = "-" + cwd.strip("/").replace("/", "-")
    d = projects / encoded
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("")
    if mtime is not None:
        import os
        os.utime(p, (mtime, mtime))
    return p


def test_codex_agent_resumes_from_codex_sessions(tmp_path, monkeypatch):
    """A codex-backed agent must check ~/.codex/sessions (via
    codex_transcript), NOT ~/.claude/projects — otherwise it loses its
    conversation on every server restart."""
    from lib import codex_transcript
    calls = {}
    def fake_find(uuid, sessions_root=None):
        calls["uuid"] = uuid
        return pathlib.Path(f"/home/example/.codex/sessions/2026/05/31/rollout-x-{uuid}.jsonl")
    monkeypatch.setattr(codex_transcript, "find_latest_jsonl", fake_find)

    results = resume_missing_sessions(
        {"elli": {"name": "Elli", "voice_id": "v",
                  "cwd": str(pathlib.Path.home()), "backend": "codex"}},
        pathlib.Path.home(),
        projects_root=tmp_path / "empty-projects",
        backend_sessions_by_session={"elli": "codex-uuid-7"})
    assert calls["uuid"] == "codex-uuid-7"
    assert results[0]["action"] == "resumed"
    assert results[0]["backend_session_id"] == "codex-uuid-7"


def test_codex_agent_fresh_when_rollout_missing(tmp_path, monkeypatch):
    from lib import codex_transcript
    monkeypatch.setattr(codex_transcript, "find_latest_jsonl",
                        lambda uuid, sessions_root=None: None)
    results = resume_missing_sessions(
        {"elli": {"name": "Elli", "voice_id": "v",
                  "cwd": str(pathlib.Path.home()), "backend": "codex"}},
        pathlib.Path.home(),
        projects_root=tmp_path / "empty-projects",
        backend_sessions_by_session={"elli": "gone-uuid"})
    assert results[0]["action"] == "fresh"
    assert results[0]["backend_session_id"] == ""


def test_agy_agent_resumes_from_antigravity_transcript(tmp_path, monkeypatch):
    from lib import agy_transcript
    monkeypatch.setattr(
        agy_transcript, "find_latest_jsonl",
        lambda uuid, brain_root=None: pathlib.Path(f"/agy/{uuid}/transcript.jsonl"),
    )
    results = resume_missing_sessions(
        {"arnold": {"name": "Arnold", "voice_id": "v",
                    "cwd": str(pathlib.Path.home()), "backend": "agy"}},
        pathlib.Path.home(),
        projects_root=tmp_path / "empty-projects",
        backend_sessions_by_session={"arnold": "agy-uuid-7"})
    assert results[0]["action"] == "resumed"
    assert results[0]["backend_session_id"] == "agy-uuid-7"


def test_resumes_with_id_when_mapping_exists(tmp_path):
    """When the DB carries a UUID for an agent and its JSONL exists,
    we resume that exact session."""
    projects = tmp_path / "projects"
    _make_jsonl(projects, str(pathlib.Path.home()), "session-abc")
    results = resume_missing_sessions(
        {"mike": {"name": "Mike", "voice_id": "v",
                  "cwd": str(pathlib.Path.home())}},
        pathlib.Path.home(),
        projects_root=projects,
        backend_sessions_by_session={"mike": "session-abc"},
    )
    assert results[0]["action"] == "resumed"
    assert results[0]["backend_session_id"] == "session-abc"


def test_fresh_when_no_uuid_bound(tmp_path):
    """No UUID in the DB and a populated projects dir for the cwd —
    must NOT bind to the newest jsonl there. Stays fresh; next /send
    pre-mints. This is the regression test for the cross-agent bleed."""
    projects = tmp_path / "projects"
    # Two pre-existing JSONLs in /home/example — neither belongs to Arnold.
    _make_jsonl(projects, str(pathlib.Path.home()), "elli-uuid", mtime=2000)
    _make_jsonl(projects, str(pathlib.Path.home()), "antoni-uuid", mtime=1000)
    results = resume_missing_sessions(
        {"arnold": {"name": "Arnold", "voice_id": "v",
                    "cwd": str(pathlib.Path.home())}},
        pathlib.Path.home(),
        projects_root=projects,
        backend_sessions_by_session={},  # arnold has no UUID
    )
    assert results[0]["action"] == "fresh"
    assert results[0]["backend_session_id"] == ""


def test_two_agents_same_cwd_dont_bleed(tmp_path):
    """The exact production scenario: Antoni active in /home/example,
    Arnold relaunched fresh in /home/example. Arnold must NOT pick up
    Antoni's UUID."""
    projects = tmp_path / "projects"
    _make_jsonl(projects, str(pathlib.Path.home()), "antoni-uuid")
    results = resume_missing_sessions(
        {"antoni": {"name": "Antoni", "voice_id": "v",
                    "cwd": str(pathlib.Path.home())},
         "arnold": {"name": "Arnold", "voice_id": "v",
                    "cwd": str(pathlib.Path.home())}},
        pathlib.Path.home(),
        projects_root=projects,
        backend_sessions_by_session={"antoni": "antoni-uuid"},
    )
    by_sid = {r["sid"]: r for r in results}
    assert by_sid["antoni"]["action"] == "resumed"
    assert by_sid["antoni"]["backend_session_id"] == "antoni-uuid"
    assert by_sid["arnold"]["action"] == "fresh"
    assert by_sid["arnold"]["backend_session_id"] == ""


def test_resumed_but_jsonl_missing_falls_to_fresh(tmp_path):
    """DB says agent had UUID X, but X.jsonl no longer exists on disk.
    Don't pretend we resumed — go fresh."""
    results = resume_missing_sessions(
        {"mike": {"name": "Mike", "voice_id": "v",
                  "cwd": str(pathlib.Path.home())}},
        pathlib.Path.home(),
        projects_root=tmp_path / "empty-projects",
        backend_sessions_by_session={"mike": "gone-uuid"},
    )
    assert results[0]["action"] == "fresh"
    assert results[0]["backend_session_id"] == ""


def test_falls_back_to_home_when_saved_cwd_gone(tmp_path, capsys):
    """The cwd-missing guard still works — if an agent's cwd has been
    deleted from disk, we realign to $HOME and log the realignment."""
    results = resume_missing_sessions(
        {"mike": {"name": "Mike", "voice_id": "v",
                  "cwd": "/this/does/not/exist"}},
        pathlib.Path.home(),
        projects_root=tmp_path / "projects-empty",
    )
    assert results[0]["ok"] is True
    assert results[0]["detail"] == str(pathlib.Path.home())
    assert "resumeCwdMissing" in capsys.readouterr().err


def test_cwd_from_project_dir_decodes():
    assert _cwd_from_project_dir(pathlib.Path("/x/-home-user-GIT-sqlit")) == \
        "/home/user/GIT/sqlit"
    assert _cwd_from_project_dir(pathlib.Path("/x/no-prefix")) == ""


def test_find_session_jsonl_prefers_saved_cwd(tmp_path):
    projects = tmp_path / "projects"
    p1 = _make_jsonl(projects, "/home/example", "abc")
    _make_jsonl(projects, "/home/example/GIT/sqlit", "abc")  # same id, other cwd
    assert find_session_jsonl("abc", "/home/example", projects) == p1


def test_find_session_jsonl_falls_back_to_global_scan(tmp_path):
    """If the saved cwd doesn't carry the JSONL, scan the whole
    projects root — claude-code rebinds the JSONL to the cwd that
    was current when the first turn ran, which can differ from the
    agent's stored cwd."""
    projects = tmp_path / "projects"
    p = _make_jsonl(projects, "/home/example/GIT/sqlit", "xyz")
    assert find_session_jsonl("xyz", "/home/example", projects) == p


def test_find_session_jsonl_returns_none_when_absent(tmp_path):
    assert find_session_jsonl("nope", "/home/example", tmp_path) is None


def test_find_session_jsonl_returns_none_for_empty_id(tmp_path):
    """The cwd-fallback that used to leak other agents' transcripts
    is gone — an empty UUID means "no JSONL", full stop."""
    projects = tmp_path / "projects"
    _make_jsonl(projects, "/home/example", "some-other-agent")
    assert find_session_jsonl("", "/home/example", projects) is None

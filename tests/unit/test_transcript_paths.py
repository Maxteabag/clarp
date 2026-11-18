"""A transcript lookup reads only its own session's file, inside its tool's store (R70).

Every file lives under a scratch home in tmp_path; a file "outside" a store is a
sibling of that store inside the same tmp_path, so nothing outside it is touched.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from lib import agy_transcript, claude_transcript, codex_transcript, grok_transcript, muse_transcript, resume


def _write(path: pathlib.Path, text: str = "{}\n") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# Ids that are not one path component, or carry a glob character, a NUL or a Windows separator.
NOT_ONE_COMPONENT = ["../outside", "a/b", "a\\b", "a\x00b", "/abs", ".", "..", "*", "?", "[ab]", "a b"]


# ---- Grok: the id is joined into a path ------------------------------------------------------

def test_grok_finds_its_own_session_and_nothing_outside_its_store(tmp_path):
    home = tmp_path / "grok"
    own = _write(grok_transcript.sessions_root(home) / "good-1" / "chat_history.jsonl")
    outside = _write(tmp_path / "outside" / "chat_history.jsonl")
    assert grok_transcript.find_latest_jsonl("good-1", home=home) == own
    assert grok_transcript.find_latest_jsonl("../../outside", home=home) is None
    assert grok_transcript.find_latest_jsonl(str(outside.parent), home=home) is None
    for bad in NOT_ONE_COMPONENT:
        assert grok_transcript.find_latest_jsonl(bad, home=home) is None


def test_grok_does_not_follow_a_session_directory_linked_outside_its_store(tmp_path):
    home = tmp_path / "grok"
    _write(tmp_path / "elsewhere" / "chat_history.jsonl")
    grok_transcript.sessions_root(home).mkdir(parents=True)
    (grok_transcript.sessions_root(home) / "linked").symlink_to(tmp_path / "elsewhere")
    assert grok_transcript.find_latest_jsonl("linked", home=home) is None


# ---- Antigravity: the id is joined into a path -----------------------------------------------

def _agy(root, ident):
    return root / ident / ".system_generated" / "logs" / "transcript.jsonl"


def test_agy_finds_its_own_conversation_and_nothing_outside_its_store(tmp_path):
    root = tmp_path / "brain"
    own = _write(_agy(root, "conv-1"))
    _write(tmp_path / "outside" / ".system_generated" / "logs" / "transcript.jsonl")
    assert agy_transcript.find_latest_jsonl("conv-1", brain_root=root) == own
    assert agy_transcript.find_latest_jsonl("../outside", brain_root=root) is None
    assert agy_transcript.find_latest_jsonl(str(tmp_path / "outside"), brain_root=root) is None
    for bad in NOT_ONE_COMPONENT:
        assert agy_transcript.find_latest_jsonl(bad, brain_root=root) is None


def test_agy_does_not_follow_a_conversation_linked_outside_its_store(tmp_path):
    root = tmp_path / "brain"
    _write(tmp_path / "elsewhere" / ".system_generated" / "logs" / "transcript.jsonl")
    root.mkdir()
    (root / "linked").symlink_to(tmp_path / "elsewhere")
    assert agy_transcript.find_latest_jsonl("linked", brain_root=root) is None


# ---- Claude: a glob of the id, and the cwd-hinted resume lookup --------------------------------

def test_claude_scan_finds_its_own_session_and_no_other(tmp_path):
    root = tmp_path / "projects"
    own = _write(root / "-work-a" / "11111111-aaaa.jsonl")
    _write(root / "-work-b" / "22222222-bbbb.jsonl")
    _write(tmp_path / "outside.jsonl")
    assert claude_transcript.scan_for_jsonl("11111111-aaaa", root) == own
    assert claude_transcript.scan_for_jsonl("*", root) is None              # another session's file
    assert claude_transcript.scan_for_jsonl("../../outside", root) is None  # leaves the store
    for bad in NOT_ONE_COMPONENT:
        assert claude_transcript.scan_for_jsonl(bad, root) is None


def test_claude_resume_lookup_stays_in_its_store(tmp_path):
    root = tmp_path / "projects"
    own = _write(root / "-work-a" / "11111111-aaaa.jsonl")
    _write(root / "-work-b" / "22222222-bbbb.jsonl")
    _write(tmp_path / "outside.jsonl")
    assert resume.find_session_jsonl("11111111-aaaa", "/work/a", root) == own
    assert resume.find_session_jsonl("11111111-aaaa", "/work/other", root) == own   # the scan fallback
    assert resume.find_session_jsonl("../../outside", "/work/a", root) is None
    assert resume.find_session_jsonl("*", "/work/other", root) is None
    for bad in NOT_ONE_COMPONENT:
        assert resume.find_session_jsonl(bad, "/work/a", root) is None


def test_claude_does_not_follow_a_project_linked_outside_its_store(tmp_path):
    root = tmp_path / "projects"
    _write(tmp_path / "elsewhere" / "33333333-cccc.jsonl")
    root.mkdir()
    (root / "-work-linked").symlink_to(tmp_path / "elsewhere")
    assert claude_transcript.scan_for_jsonl("33333333-cccc", root) is None
    assert claude_transcript.find_latest_jsonl("33333333-cccc", root) is None        # the index
    assert resume.find_session_jsonl("33333333-cccc", "/work/linked", root) is None   # the cwd's own dir
    assert resume.find_session_jsonl("33333333-cccc", "/work/other", root) is None    # the scan fallback


# ---- Codex: a recursive glob of the id, newest wins --------------------------------------------

def _rollout(root, stamp, ident):
    return _write(root / "2026" / "11" / "18" / f"rollout-2026-11-18T{stamp}-{ident}.jsonl")


def test_codex_finds_its_own_rollout_and_never_another_sessions(tmp_path):
    root = tmp_path / "sessions"
    own = _rollout(root, "09-00-00", "aaaa-1111")
    other = _rollout(root, "10-00-00", "bbbb-1111")
    os.utime(other, (2_000_000_000, 2_000_000_000))             # the other session is the newest
    assert codex_transcript.find_latest_jsonl("aaaa-1111", sessions_root=root) == own
    assert codex_transcript.find_latest_jsonl("*", sessions_root=root) is None
    assert codex_transcript.find_latest_jsonl("1111", sessions_root=root) is None   # a tail of both ids
    for bad in NOT_ONE_COMPONENT:
        assert codex_transcript.find_latest_jsonl(bad, sessions_root=root) is None


def test_codex_does_not_follow_a_day_linked_outside_its_store(tmp_path):
    root = tmp_path / "sessions"
    _write(tmp_path / "elsewhere" / "rollout-2026-11-18T09-00-00-cccc-1111.jsonl")
    (root / "2026" / "11").mkdir(parents=True)
    (root / "2026" / "11" / "19").symlink_to(tmp_path / "elsewhere")
    assert codex_transcript.find_latest_jsonl("cccc-1111", sessions_root=root) is None


# ---- Muse: its own stricter pattern, plus the store -------------------------------------------

def test_muse_keeps_its_pattern_and_does_not_follow_a_link_outside_its_store(tmp_path):
    home = tmp_path / "muse"
    own = _write(muse_transcript.sessions_root(home) / "2026" / "11" / "18" / "abcdef12-0001" / "session.jsonl")
    assert muse_transcript.find_latest_jsonl("abcdef12-0001", home=home) == own
    _write(muse_transcript.sessions_root(home) / "2026" / "11" / "18" / "abcdef12-0003\n" / "session.jsonl")
    assert muse_transcript.find_latest_jsonl("abcdef12-0003\n", home=home) is None    # a full match only
    _write(tmp_path / "elsewhere" / "session.jsonl")
    (muse_transcript.sessions_root(home) / "2026" / "11" / "18" / "abcdef12-0002").symlink_to(tmp_path / "elsewhere")
    assert muse_transcript.find_latest_jsonl("abcdef12-0002", home=home) is None


# ---- the check itself and its log ---------------------------------------------------------------

def test_a_refusal_logs_the_backend_only_and_never_raises(tmp_path, monkeypatch):
    """Every lookup refuses an id that is not one component itself, before it touches the disk:
    one line per refusal, naming the backend only."""
    from lib import transcript_paths
    lines = []
    monkeypatch.setattr(transcript_paths, "log", lambda event, detail="": lines.append((event, detail)))
    lookups = {
        "grok": lambda bad: grok_transcript.find_latest_jsonl(bad, home=tmp_path / "grok"),
        "agy": lambda bad: agy_transcript.find_latest_jsonl(bad, brain_root=tmp_path / "brain"),
        "codex": lambda bad: codex_transcript.find_latest_jsonl(bad, sessions_root=tmp_path),
        "claude": lambda bad: claude_transcript.scan_for_jsonl(bad, tmp_path / "projects"),
        "claude-resume": lambda bad: resume.find_session_jsonl(bad, "/work/a", tmp_path / "projects"),
        "claude-index": lambda bad: claude_transcript.find_latest_jsonl(bad, tmp_path / "projects"),
    }
    for backend in ("grok", "brain", "projects"):
        (tmp_path / backend).mkdir()
    for name, lookup in lookups.items():
        lines.clear()
        for bad in NOT_ONE_COMPONENT:
            assert lookup(bad) is None
        backend = name.split("-")[0]
        assert lines == [("transcriptSessionIdRefused", f"backend={backend}")] * len(NOT_ONE_COMPONENT), name


@pytest.mark.parametrize("ident", ["bs-golden", "11111111-2222-3333-4444-555555555555", "ses_ABC.def-1", "a" * 128])
def test_well_formed_ids_pass_the_rule(ident):
    from lib import transcript_paths
    assert transcript_paths.session_id_ok(ident)


@pytest.mark.parametrize("ident", [*NOT_ONE_COMPONENT, "a" * 129, "-a", "_a", None, 5])
def test_ids_that_are_not_one_component_fail_the_rule(ident):
    from lib import transcript_paths
    assert not transcript_paths.session_id_ok(ident)


def test_antigravity_forgets_nothing_for_an_id_that_is_not_one_component(tmp_path):
    from lib.backend import agy
    store = tmp_path / ".gemini" / "antigravity-cli"
    keep = _write(store / "conversations" / "keep.db")
    _write(store / "brain" / "keep" / "x")
    agy._forget_conversations(tmp_path, "..")
    agy._forget_conversations(tmp_path, "*")
    assert keep.is_file() and (store / "brain" / "keep" / "x").is_file() and store.is_dir()


def test_binding_refuses_an_id_that_is_not_one_component(tmp_path):
    from lib import agents
    agent = agents.create_agent(persona="Theo", voice_id="v", cwd=str(tmp_path), session="theo")
    with pytest.raises(ValueError, match="one path component"):
        agents.bind_backend_session(agent, "../outside")
    agents.bind_backend_session(agent, "11111111-2222-3333-4444-555555555555")


def test_muse_refusing_an_id_by_its_own_pattern_logs_the_one_line_too(tmp_path, monkeypatch):
    """Muse's own, narrower pattern refused silently while only its containment refusal logged."""
    from lib import backends, transcript_paths
    lines = []
    monkeypatch.setattr(transcript_paths, "log", lambda event, detail="": lines.append((event, detail)))
    assert muse_transcript.find_latest_jsonl("not-a-muse-id", home=tmp_path / "muse") is None
    assert muse_transcript.find_latest_jsonl("-abcdef12", home=tmp_path / "muse") is None
    assert backends.get("muse")._reclaim_session("not-a-muse-id", agent_id="a", session="s") == "unverifiable"
    assert lines == [("transcriptSessionIdRefused", "backend=muse")] * 3

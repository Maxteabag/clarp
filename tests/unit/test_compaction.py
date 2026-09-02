"""Tests for compaction.compact_session — the guard logic before we ever
spawn a tmux. We never actually launch a CLI here; we monkeypatch the deps and
assert the early-return decisions, plus that a valid call starts a worker."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import compaction  # noqa: E402
from lib import backends  # noqa: E402


def _patch(monkeypatch, *, agent, bsid="bsid-1", busy=False, on_path=True,
           run=None):
    monkeypatch.setattr(compaction.agents_db, "get_by_session", lambda s: agent)
    monkeypatch.setattr(compaction.agents_db, "live_backend_session",
                        lambda a: bsid)
    monkeypatch.setattr(compaction.backends, "active_handles",
                        lambda b, a: (["h"] if busy else []))
    monkeypatch.setattr(compaction.shutil, "which",
                        lambda x: ("/usr/bin/" + x) if on_path else None)
    # Never spawn a real thread/tmux in tests.
    monkeypatch.setattr(compaction.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    compaction._active.clear()


def test_no_such_agent(monkeypatch):
    _patch(monkeypatch, agent=None)
    assert compaction.compact_session("x")["ok"] is False


def test_no_live_session(monkeypatch):
    _patch(monkeypatch, agent={"agent_id": "a", "backend": "claude"}, bsid="")
    out = compaction.compact_session("x")
    assert out["ok"] is False and "no live session" in out["error"]


def test_busy_agent_refused(monkeypatch):
    _patch(monkeypatch, agent={"agent_id": "a", "backend": "claude"}, busy=True)
    out = compaction.compact_session("x")
    assert out["ok"] is False and "busy" in out["error"]


def test_missing_binary_refused(monkeypatch):
    _patch(monkeypatch, agent={"agent_id": "a", "backend": "claude"},
           on_path=False)
    out = compaction.compact_session("x")
    assert out["ok"] is False and "PATH" in out["error"]


def test_valid_claude_starts(monkeypatch):
    _patch(monkeypatch, agent={"agent_id": "a", "backend": "claude",
                               "cwd": "/tmp"})
    out = compaction.compact_session("sammy")
    assert out["ok"] is True and out["status"] == "started"
    assert out["backend"] == backends.CLAUDE
    assert compaction.is_compacting("sammy") is True


def test_compact_command_map_per_backend():
    assert compaction._COMPACT[backends.CLAUDE][1] == "/compact"
    assert compaction._COMPACT[backends.CODEX][1] == "/compact"
    assert compaction._COMPACT[backends.AGY][1] == "/compress"
    assert compaction._COMPACT[backends.GROK][1] == "/compact"

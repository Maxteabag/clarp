"""HOOK-01: no hook swallows a failure silently, and none changes its output to say so.

A hook catches broad exceptions so it never fails the user's turn; each such
handler now reports through plugin/hooks/_hook_report.report: one event with
the exception class (and SQLite's error name), no message text, or a line in
the hook log when the event log is unavailable. Nothing goes to stdout or
stderr, so no runner can show it to the model or the apps.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import pathlib
import sqlite3

import pytest

from lib import eventlog, turn_lifecycle
from lib import agents as agents_db
from lib.paths import RuntimePaths

HOOKS = pathlib.Path(__file__).resolve().parents[2] / "plugin" / "hooks"
_RULE = pathlib.Path(__file__).resolve().parent / "test_swallowed_excepts.py"
_spec = importlib.util.spec_from_file_location("swallowed_rule_for_hooks", _RULE)
_rule = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rule)


def _load(name: str, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _locked() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked: SECRET tool input")
    exc.sqlite_errorname = "SQLITE_BUSY"
    return exc


@pytest.fixture
def emitted(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(eventlog, "emit", lambda source, event, **kw: calls.append((source, event, kw)))
    return calls


def test_no_hook_swallows_a_broad_except_silently():
    found = {p.name: _rule.swallowed(ast.parse(p.read_text())) for p in sorted(HOOKS.glob("*.py"))}
    assert {name: lines for name, lines in found.items() if lines} == {}


def test_a_failed_tool_started_write_is_reported_without_content(monkeypatch, capsys, emitted):
    # The registered bug: hook_transition's 'database is locked' left no trace.
    hook = _load("tool_activity", monkeypatch)
    monkeypatch.setattr(agents_db, "resolve_for_hook", lambda **kw: {"agent_id": "a1", "session": "s"})

    def fail(*a, **kw):
        raise _locked()
    monkeypatch.setattr(turn_lifecycle, "hook_transition", fail)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "b1", "tool_name": "Read", "tool_input": {"file_path": "/x"}})))

    assert hook.main() == 0
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")
    assert [(s, e, kw["detail"]) for s, e, kw in emitted] == [
        ("toolstart_hook", "recordFail", {"error": "OperationalError", "code": "SQLITE_BUSY"})]
    assert "SECRET" not in repr(emitted)


def test_unreadable_stdin_is_reported_and_the_hook_still_exits_zero(monkeypatch, capsys, emitted):
    hook = _load("tool_finished", monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
    assert [(s, e, kw["detail"]["error"]) for s, e, kw in emitted
            if e != "log"] == [("toolend_hook", "stdinFail", "JSONDecodeError")]


def test_when_the_event_log_fails_the_report_goes_to_the_hook_log(monkeypatch, tmp_path, capsys):
    report = _load("_hook_report", monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    def broken(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(eventlog, "emit", broken)

    report.report("toolstart_hook", "recordFail", _locked())

    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")
    line = RuntimePaths.from_home(tmp_path).hook_log.read_text()
    assert "toolstart_hook" in line and "recordFail OperationalError SQLITE_BUSY" in line
    assert "event log unavailable: OperationalError" in line
    assert "SECRET" not in line and "disk I/O" not in line

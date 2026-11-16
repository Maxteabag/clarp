"""HOOK-01: no hook swallows a failure silently, and none changes its output to say so.

A failure first appends its content-free private line, then optionally mirrors
its exception class (and SQLite error name) into telemetry. Detailed capture
never gates the mandatory line. Nothing is emitted on the hook's stdout/stderr,
including deferred sink diagnostics at process exit.
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
    assert "SECRET" not in line and "disk I/O" not in line


def _homeless(monkeypatch):
    # A minimal container: HOME unset and no passwd entry, so Path.home()
    # raises RuntimeError (review A2 F1/F2).
    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(no_home))
    monkeypatch.setattr("os.path.expanduser", lambda path: path)
    for name in ("_clarp_lib", "_hook_report"):
        monkeypatch.delitem(__import__("sys").modules, name, raising=False)


def test_report_never_raises_when_the_event_log_and_the_home_lookup_both_fail(monkeypatch, capsys):
    report = _load("_hook_report", monkeypatch)
    _homeless(monkeypatch)

    def broken(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(eventlog, "emit", broken)

    assert report.report("toolstart_hook", "recordFail", _locked()) is None
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")


def test_agy_state_stays_fail_open_without_a_home(monkeypatch, capsys):
    import runpy
    _homeless(monkeypatch)
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.delenv("CLAUDE_PWA_DB", raising=False)
    monkeypatch.delenv("CLARP_AGY_MANAGED_TURN", raising=False)
    monkeypatch.setattr("sys.argv", ["agy_state.py", "PreModel"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"conversationId": "c1"})))

    runpy.run_path(str(HOOKS / "agy_state.py"), run_name="__main__")

    assert capsys.readouterr().out == "{}\n"


@pytest.mark.parametrize("capture", ["off", "excluded", "on"])
def test_real_hook_report_storage_obeys_capture_without_losing_failure(
        monkeypatch, tmp_path, capsys, capture):
    from lib import diagnostics_settings, settings_store, telemetry

    if capture == "off":
        settings_store.set_text(diagnostics_settings.KEY, "")
        diagnostics_settings.reset_for_tests()
    else:
        diagnostics_settings.update({"enabled": True,
                                     "categories": ["agents"] if capture == "on" else ["voice"]})
    before = diagnostics_settings.get()
    reporter = _load("_hook_report", monkeypatch)
    log = RuntimePaths.from_home(tmp_path).hook_log
    at_insert = []
    def observe_insert(sql):
        if "INSERT INTO DIAGNOSTIC_EVENTS" in sql.upper():
            at_insert.append(log.is_file() and len(log.read_text().splitlines()) == 1)
    telemetry.conn().set_trace_callback(observe_insert)
    assert reporter.report("toolstart_hook", "recordFail", _locked()) is None
    telemetry.conn().set_trace_callback(None)
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    rows = telemetry.conn().execute(
        "SELECT source,event,level,detail FROM diagnostic_events").fetchall()
    if capture == "on":
        assert len(rows) == 1
        assert tuple(rows[0][:3]) == ("toolstart_hook", "recordFail", "error")
        assert json.loads(rows[0]["detail"]) == {"error": "OperationalError", "code": "SQLITE_BUSY"}
        assert at_insert == [True]
    else:
        assert rows == []
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert "toolstart_hook" in lines[0] and "recordFail OperationalError SQLITE_BUSY" in lines[0]
    assert "SECRET" not in lines[0] and "locked" not in lines[0]
    assert diagnostics_settings.get() == before


@pytest.mark.parametrize("unwritable", [False, True])
def test_real_hook_report_sink_import_failure_stays_private(
        monkeypatch, tmp_path, capsys, unwritable):
    import builtins
    from lib import diagnostics_settings, telemetry

    diagnostics_settings.update({"enabled": True, "categories": ["agents"]})
    reporter = _load("_hook_report", monkeypatch)
    log = RuntimePaths.from_home(tmp_path).hook_log
    if unwritable:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.mkdir()  # Real IsADirectoryError; neither writer can be used.
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "lib.eventlog":
            raise ImportError("SECRET sink unavailable")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert reporter.report("toolend_hook", "resolveFail", _locked()) is None
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    assert telemetry.conn().execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0] == 0
    if not unwritable:
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        assert "toolend_hook" in lines[0] and "resolveFail OperationalError SQLITE_BUSY" in lines[0]
        assert "SECRET" not in lines[0]


def test_real_policy_cache_transition_cannot_drop_the_hook_report(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from lib import diagnostics_settings, settings_store, telemetry

    diagnostics_settings.update({"enabled": True, "categories": ["agents"]})
    telemetry.conn()  # Initialize the real sink before controlling only the policy clock.
    settings_store.set_text(diagnostics_settings.KEY, '{"enabled":false,"categories":[]}')
    diagnostics_settings._CACHED_AT = 100.0
    reads = []
    def cache_clock():
        value = 101.9 if not reads else 102.1
        reads.append(value)
        return value
    monkeypatch.setattr(diagnostics_settings, "time", SimpleNamespace(monotonic=cache_clock))
    reporter = _load("_hook_report", monkeypatch)
    assert reporter.report("toolstart_hook", "recordFail", _locked()) is None
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    rows = telemetry.conn().execute(
        "SELECT COUNT(*) FROM diagnostic_events WHERE source='toolstart_hook' AND event='recordFail'").fetchone()[0]
    log = RuntimePaths.from_home(tmp_path).hook_log
    lines = log.read_text().splitlines() if log.exists() else []
    assert len(lines) == 1
    assert rows == 1
    assert len(reads) == 1


@pytest.mark.parametrize("log_unwritable", [False, True])
def test_real_record_failure_keeps_trace_when_event_writers_both_fail(
        monkeypatch, tmp_path, capsys, log_unwritable):
    from lib import diagnostics_settings, telemetry

    diagnostics_settings.update({"enabled": True, "categories": ["agents"]})
    agent = agents_db.create_agent(persona="Hook test", voice_id="V", cwd=str(tmp_path), session="hook-test")
    agents_db.bind_backend_session(agent, "hook-test-uuid")
    hook = _load("tool_activity", monkeypatch)
    def fail_transition(*args, **kwargs):
        raise _locked()
    monkeypatch.setattr(turn_lifecycle, "hook_transition", fail_transition)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "hook-test-uuid", "tool_name": "Read", "tool_input": {"file_path": "/SECRET/project"}})))
    log = RuntimePaths.from_home(tmp_path).hook_log
    if log_unwritable:
        log.mkdir(parents=True)
    broken_jsonl = tmp_path / "not-a-jsonl-directory"
    broken_jsonl.write_text("a file, not a directory")
    monkeypatch.setattr(eventlog, "LOG_DIR", broken_jsonl)
    connection = telemetry.conn()
    connection.execute("PRAGMA busy_timeout=1")
    blocker = sqlite3.connect(telemetry.TELEMETRY_PATH, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        assert hook.main() == 0
    finally:
        blocker.rollback()
        blocker.close()
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    assert connection.execute(
        "SELECT COUNT(*) FROM diagnostic_events WHERE source='toolstart_hook' AND event='recordFail'").fetchone()[0] == 0
    if not log_unwritable:
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        assert "toolstart_hook" in lines[0] and "recordFail OperationalError SQLITE_BUSY" in lines[0]
        assert "SECRET" not in lines[0] and "database is locked" not in lines[0]


def test_real_report_with_unwritable_hook_log_still_writes_event(monkeypatch, tmp_path, capsys):
    from lib import diagnostics_settings, telemetry

    diagnostics_settings.update({"enabled": True, "categories": ["agents"]})
    reporter = _load("_hook_report", monkeypatch)
    log = RuntimePaths.from_home(tmp_path).hook_log
    log.mkdir(parents=True)
    assert reporter.report("toolend_hook", "resolveFail", _locked()) is None
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    assert telemetry.conn().execute(
        "SELECT COUNT(*) FROM diagnostic_events WHERE source='toolend_hook' AND event='resolveFail'").fetchone()[0] == 1


def test_real_writer_failure_stays_private_through_hook_exit(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    child = textwrap.dedent('''
        import io,json,pathlib,sqlite3,sys
        root=pathlib.Path(sys.argv[1]);scratch=pathlib.Path(sys.argv[2])
        sys.path[:0]=[str(root/'server'),str(root/'plugin/hooks')]
        from lib import db,agents,diagnostics_settings,telemetry,eventlog,turn_lifecycle
        import tool_activity
        db.run_as_service('tests');db.conn()
        diagnostics_settings.update({'enabled':True,'categories':['agents']})
        agent=agents.create_agent(persona='Hook test',voice_id='V',cwd=str(scratch),session='exit-test')
        agents.bind_backend_session(agent,'exit-test-uuid')
        def failure(*a,**kw):
            error=sqlite3.OperationalError('SECRET tool input')
            error.sqlite_errorname='SQLITE_BUSY'
            raise error
        turn_lifecycle.hook_transition=failure
        sys.stdin=io.StringIO(json.dumps({'session_id':'exit-test-uuid','tool_name':'Read','tool_input':{}}))
        connection=telemetry.conn();connection.execute('PRAGMA busy_timeout=1')
        invalid=scratch/'not-a-directory';invalid.write_text('x');eventlog.LOG_DIR=invalid
        lock=sqlite3.connect(telemetry.TELEMETRY_PATH,isolation_level=None)
        try:
            lock.execute('BEGIN IMMEDIATE')
            assert tool_activity.main()==0
        finally:
            lock.rollback();lock.close()
        sys.stdout.write('caller-output')
    ''')
    result = subprocess.run([sys.executable, "-c", child, str(HOOKS.parents[1]), str(tmp_path)],
                            env={**os.environ}, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == b"caller-output"
    assert result.stderr == b""
    lines = RuntimePaths.from_home(tmp_path).hook_log.read_text().splitlines()
    assert len(lines) == 1 and "recordFail OperationalError SQLITE_BUSY" in lines[0]
    assert "SECRET" not in lines[0]

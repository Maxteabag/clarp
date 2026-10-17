"""The retained-importer gate in tests/conftest.py precedes every per-test
state mutation by explicit fixture dependency, not by definition order (which
pytest does not guarantee).

Shown by an inner pytest session in a child process that loads this repo's
tests/conftest.py, records every fixture pytest sets up, and moves the gate to
the END of each test's fixture closure: an order pytest may choose for
independent same-scope fixtures. Only a declared dependency still puts the gate
first. The child's PATH holds only system directories and it has its own HOME.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[2]
GATE = "_transcript_importer_quiescent"
# Below the suite's per-test --timeout=60 (pyproject addopts), so this bound, not
# pytest-timeout, ends a stuck child and its cleanup below still runs.
INNER_TIMEOUT_S = 40
# Per-test fixtures that set environment, log, config or DB state.
MUTATORS = ("_isolate_host_service_commands", "_no_real_agy_cli", "_guard_backend_clis",
            "_isolate_agy_skills", "_isolate_xdg", "_isolated_db")


def _inner_session(tmp_path, *, refuse: bool) -> tuple[subprocess.CompletedProcess, list[str]]:
    record = tmp_path / "fixture-setups.txt"
    suite = tmp_path / "inner"
    suite.mkdir()
    (suite / "conftest.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path[:0] = [{str(REPO)!r}, {str(REPO / 'server')!r}]
        import pytest
        pytest_plugins = ["tests.conftest"]        # the session setup under test
        RECORD = {str(record)!r}
        REFUSE = {refuse!r}

        def pytest_sessionstart(session):
            if REFUSE:
                # An earlier test's teardown failed closed and recovery is refused.
                from lib import transcript_import_cache as importer
                importer._resetting = True
                importer.reset_for_tests = lambda *a, **k: False

        def pytest_collection_modifyitems(items):
            for item in items:
                names = item.fixturenames
                names.remove({GATE!r})
                names.append({GATE!r})               # last: only dependencies can hoist it

        @pytest.hookimpl(hookwrapper=True)
        def pytest_fixture_setup(fixturedef, request):
            with open(RECORD, "a") as handle:
                handle.write(fixturedef.argname + "\\n")
            yield
    """))
    (suite / "test_inner.py").write_text("def test_inner():\n    pass\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PATH": f"/usr/bin{os.pathsep}/bin", "HOME": str(home)}
    for key in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST", "CLARP_TEST_GUARD_DIR"):
        env.pop(key, None)
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", "-n", "0",
            "--rootdir", str(suite), "--basetemp", str(tmp_path / "inner-tmp"), str(suite)]
    # The child and everything it starts get their own process group, which is
    # killed whenever this helper leaves without a normal exit (timeout, or a
    # pytest-timeout or KeyboardInterrupt raised into communicate()), and always
    # reaped: nothing it owns outlives the test.
    child = subprocess.Popen(argv, cwd=suite, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=INNER_TIMEOUT_S)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)    # the group outlives a child that exited
        except ProcessLookupError:
            pass
        if child.returncode is None:
            child.communicate()
    done = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    setups = record.read_text().split() if record.exists() else []
    return done, setups


def test_a_refused_recovery_sets_up_no_per_test_state(tmp_path):
    done, setups = _inner_session(tmp_path, refuse=True)
    out = done.stdout + done.stderr
    assert "transcript importer from an earlier test is still running" in out, out
    assert "1 error" in out, out
    assert GATE in setups, setups
    assert [name for name in MUTATORS if name in setups] == [], setups


def test_without_a_retained_importer_every_mutator_is_set_up_after_the_gate(tmp_path):
    """The recording is not vacuous: an ordinary run sets up every mutator, and
    each one after the gate even though the gate was moved last."""
    done, setups = _inner_session(tmp_path, refuse=False)
    out = done.stdout + done.stderr
    assert "1 passed" in out, out
    assert all(name in setups for name in MUTATORS), setups
    assert all(setups.index(GATE) < setups.index(name) for name in MUTATORS), setups

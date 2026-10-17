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
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
GATE = "_transcript_importer_quiescent"
# Below the suite's per-test --timeout=60 (pyproject addopts): the child's 40 s
# plus the bounded reap end before pytest-timeout would, and the cleanup below
# runs in either case.
INNER_TIMEOUT_S = 40
REAP_TIMEOUT_S = 5
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
    # The child leads its own process group. Its exit is awaited WITHOUT reaping
    # it (WNOWAIT), so until the reap below its pid, and with it the group id,
    # cannot be reused: the group is killed while it is still ours, on every
    # exit (normal, the 40 s limit, or pytest-timeout raised into the wait), and
    # only then is the leader reaped, within a bounded wait. Output goes to files,
    # so no pipe can stall the child. Descendants that left the group with their
    # own setsid are outside this cleanup; the sandbox bounds those.
    out_path, err_path = tmp_path / "inner-stdout.txt", tmp_path / "inner-stderr.txt"
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        child = subprocess.Popen(argv, cwd=suite, env=env, stdout=out_f, stderr=err_f,
                                 start_new_session=True)
    try:
        deadline = time.monotonic() + INNER_TIMEOUT_S
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            assert time.monotonic() < deadline, f"inner pytest still running after {INNER_TIMEOUT_S} s"
            time.sleep(0.05)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)    # the leader is unreaped: the group is still ours
        except ProcessLookupError:
            pass
        child.wait(timeout=REAP_TIMEOUT_S)
    done = subprocess.CompletedProcess(argv, child.returncode, out_path.read_text(), err_path.read_text())
    setups = record.read_text().split() if record.exists() else []
    return done, setups


def test_a_refused_recovery_sets_up_no_per_test_state(tmp_path):
    done, setups = _inner_session(tmp_path, refuse=True)
    out = done.stdout + done.stderr
    assert "transcript importer from an earlier test is still running" in out, out   # the refusal
    assert GATE in setups, setups                                                      # the gate ran
    assert [name for name in MUTATORS if name in setups] == [], setups                 # the invariant
    assert done.returncode == 1 and "1 error" in out, out                              # exact status


def test_without_a_retained_importer_every_mutator_is_set_up_after_the_gate(tmp_path):
    """The recording is not vacuous: an ordinary run sets up every mutator, and
    each one after the gate even though the gate was moved last."""
    done, setups = _inner_session(tmp_path, refuse=False)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "1 passed" in out, out
    assert all(name in setups for name in MUTATORS), setups
    assert all(setups.index(GATE) < setups.index(name) for name in MUTATORS), setups

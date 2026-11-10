"""The refactor program's landing gate fails closed: on a failure element the
counters do not report, on a failed CI step that no quarantined test explains,
and on a run whose results could not be downloaded."""
import importlib.util
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gate_judge", ROOT / "tools/refactor/gate_judge.py")
gate_judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_judge)

GREEN_JS = '<testsuites tests="1" failures="0" errors="0"><testsuite tests="1" failures="0" errors="0">' \
           '<testcase classname="a.test.js" name="ok"/></testsuite></testsuites>'


def _steps(**failed):
    names = ["Run uv sync --frozen --group dev", *gate_judge.TEST_STEPS, "Run actions/upload-artifact@v4"]
    steps = [{"name": n, "status": "completed", "conclusion": failed.get(n, "success")} for n in names]
    return {"jobs": [{"name": "tests", "status": "completed", "conclusion": "failure" if failed else "success",
                      "steps": steps}]}


def _results(tmp_path, python_xml, quarantine="", jobs=None):
    python_step = "Python unit, integration and contract tests"
    (tmp_path / "jobs.json").write_text(json.dumps(jobs or _steps(**{python_step: "failure"})))
    (tmp_path / "python.xml").write_text(python_xml)
    (tmp_path / "javascript.xml").write_text(GREEN_JS)
    (tmp_path / "quarantine.txt").write_text(quarantine)
    return tmp_path


def _suite(counters, case):
    return f'<testsuites><testsuite {counters}><testcase classname="tests.unit.test_a" name="ok"/>{case}' \
           '</testsuite></testsuites>'


FAILING = '<testcase classname="tests.unit.test_a" name="test_b[x]"><failure message="m"/></testcase>'


def test_a_failure_element_fails_the_gate_even_when_the_counters_say_zero(tmp_path):
    out = _results(tmp_path, _suite('tests="2" failures="0" errors="0"', FAILING))
    lines, ok = gate_judge.judge(out, "success", "s", "s")
    assert not ok and "summary says failures=0 but the report has 1" in lines[-1]


def test_missing_or_negative_counters_fail_the_gate(tmp_path):
    for counters in ('name="pytest"', 'tests="-1" failures="0" errors="0"', 'tests="2" failures="-1" errors="0"'):
        out = _results(tmp_path, _suite(counters, ""))
        assert not gate_judge.judge(out, "success", "s", "s")[1], counters


def test_only_quarantined_failures_are_tolerated(tmp_path):
    listed = "tests/unit/test_a.py::test_b[x]  # load\n"
    out = _results(tmp_path, _suite('tests="2" failures="1" errors="0"', FAILING), listed)
    assert gate_judge.judge(out, "failure", "s", "s")[1]
    assert not gate_judge.judge(out, "cancelled", "s", "s")[1]
    assert not gate_judge.judge(out, "failure", "other", "s")[1]
    for step in ("Run actions/upload-artifact@v4", "JavaScript behavior and contract tests"):
        jobs = _steps(**{"Python unit, integration and contract tests": "failure", step: "failure"})
        out = _results(tmp_path, _suite('tests="2" failures="1" errors="0"', FAILING), listed, jobs)
        lines, ok = gate_judge.judge(out, "failure", "s", "s")
        assert not ok and f"step '{step}' failed outside the quarantine" in lines[-1]
    out = _results(tmp_path, _suite('tests="2" failures="1" errors="0"', FAILING), listed)
    for jobs in ({"jobs": []}, _steps(), {"jobs": [{**_steps()["jobs"][0], "status": "in_progress"}]}):
        (out / "jobs.json").write_text(json.dumps(jobs))
        assert not gate_judge.judge(out, "failure", "s", "s")[1], jobs
    for conclusion in (None, "mystery"):
        jobs = _steps(**{"Python unit, integration and contract tests": "failure",
                         "Run actions/upload-artifact@v4": conclusion})
        (out / "jobs.json").write_text(json.dumps(jobs))
        assert not gate_judge.judge(out, "failure", "s", "s")[1], conclusion
    jobs = _steps(**{"Python unit, integration and contract tests": "failure"})
    del jobs["jobs"][0]["steps"][-1]["conclusion"]
    (out / "jobs.json").write_text(json.dumps(jobs))
    assert not gate_judge.judge(out, "failure", "s", "s")[1]
    (out / "jobs.json").unlink()
    assert not gate_judge.judge(out, "failure", "s", "s")[1]
    real = FAILING.replace("test_b[x]", "test_c")
    out = _results(tmp_path, _suite('tests="3" failures="2" errors="0"', FAILING + real), listed)
    assert not gate_judge.judge(out, "failure", "s", "s")[1]


def test_a_failed_download_fails_the_gate_despite_green_results_on_disk(tmp_path):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/heads/refactor/ci/t"], check=True)
    base = tmp_path / "results"
    _results(base.mkdir() or base, _suite('tests="1" failures="0" errors="0"', ""))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"""#!/bin/sh
case "$*" in
  "auth token"*) echo token;;
  "run view"*"--json status"*) echo completed;;
  "run view"*) echo "success {sha} 1";;
  "run download"*) exit 1;;
  *) echo "unexpected gh $*" >&2; exit 3;;
esac
""")
    (bin_dir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GATE_RUN": "7", "GATE_DIR": str(base),
           "GATE_POLL_S": "0", "GH_TOKEN": "t"}
    result = subprocess.run(["bash", str(ROOT / "tools/refactor/gate.sh"), "refactor/ci/t", sha], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "could not download test-results" in result.stderr
    assert "PASS" not in result.stdout

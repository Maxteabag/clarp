"""install-host holds an install good only once the run of the Host that answered has finished
its start (issue 935).

The Host answers that it is there before its start is done. install-host's host_started is run
here alone, taken from the script, with a stand-in curl that answers GET /host/start from a list
of answers, one per call: nothing is installed and no Host runs.
"""
import json
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTALL_HOST = ROOT / "server-rs/tools/install-host"

CURL = """#!/bin/sh
# Stand-in curl: answers the next line of $STANDIN/answers; "-" is no answer (refused).
n=$(cat "$STANDIN/calls" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STANDIN/calls"
line=$(sed -n "$((n + 1))p" "$STANDIN/answers")
[ -z "$line" ] && line=$(tail -n 1 "$STANDIN/answers")
[ "$line" = "-" ] && exit 7
printf '%s' "$line"
"""


def run(tmp_path, answers, seconds_per_call=None):
    standin = tmp_path / "standin"
    standin.mkdir()
    (standin / "curl").write_text(CURL)
    (standin / "curl").chmod(0o755)
    (standin / "answers").write_text("\n".join(
        "-" if a is None else json.dumps(a) for a in answers) + "\n")
    script = ('eval "$(sed -n \'/^host_started() {/,/^}/p\' "$1")"; '
              'SECONDS=0; host_started 127.0.0.1:1 token')
    env = dict(os.environ, PATH=f"{standin}:{os.environ['PATH']}", STANDIN=str(standin))
    began = time.monotonic()
    done = subprocess.run(["bash", "-c", script, "host_started", str(INSTALL_HOST)],
                          capture_output=True, text=True, env=env, timeout=120)
    return done, time.monotonic() - began


def test_a_run_that_reaches_started_is_held_good(tmp_path):
    answers = [None, {"instance": "a", "stage": "starting"}, {"instance": "a", "stage": "agents"},
               {"instance": "a", "stage": "started"}]
    done, _ = run(tmp_path, answers)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "a"


def test_a_late_failure_and_another_run_is_no_success(tmp_path):
    # The first run answered and then ended in its start; systemd started another one.
    answers = [{"instance": "a", "stage": "starting"}, None, {"instance": "b", "stage": "started"}]
    done, _ = run(tmp_path, answers)
    assert done.returncode == 1
    assert "another run answers: a did not finish its start, b was started" in done.stderr


def test_a_host_from_before_says_no_stage_and_its_answer_is_its_start_done(tmp_path):
    done, _ = run(tmp_path, [None, None, {"opening": 0}])
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "before-935"


def test_a_run_that_stops_answering_before_its_start_is_done_fails(tmp_path):
    # After its last answer the stand-in stays refused: no more than about 30 seconds are waited.
    done, took = run(tmp_path, [{"instance": "a", "stage": "agents"}, None])
    assert done.returncode == 1
    assert "the run a stopped answering before its start was done (agents)" in done.stderr
    assert took < 60

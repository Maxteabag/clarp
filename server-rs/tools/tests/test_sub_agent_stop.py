"""`clarp-sub-agent stop NAME` stops a Clarp helper through the Host, not only its watcher.

Stopping the watcher alone left the helper's turn running, and its parent ended the helper's
Claude Code by its process id instead: the turn failed as "claude exited with code 143", with
nothing saying who ended it (26 Nov 2026). The watcher goes only once the Host says the Stop
held the helper's queue. A plain worker is stopped by its unit. A watcher that
sees its helper was archived ends on its own.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[3] / "skills"

HELPER = {"session": "r1-helper", "agent_id": "h1", "role": "helper", "parent_agent_id": "p1",
          "archived_at": None}
PARENT = {"session": "the-parent", "agent_id": "p1", "role": "agent", "archived_at": None}
STOPPED = {"ok": True, "terminated": 1, "queue_paused": True, "goals_paused": 0,
           "stop_actor": "agent:the-parent", "stop_actor_verified": True}


def load():
    spec = importlib.util.spec_from_file_location(
        "sub_agent_stop_under_test", SKILLS / "clarp-sub-agents/scripts/sub_agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stub(bin_dir, name, calls, answers, err="", code=0):
    """An executable on PATH that records its arguments, one JSON line per call, and prints the
    answer kept for them: by `stop`, or by the session `agent helper-state` is asked about."""
    path = bin_dir / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"open({str(calls)!r}, 'a').write(json.dumps([{name!r}] + args) + '\\n')\n"
        f"answers = json.loads({json.dumps(answers)!r})\n"
        "key = args[2] if args[:2] == ['agent', 'helper-state'] else (args[0] if args else '')\n"
        "if key not in answers:\n"
        "    sys.stderr.write('HTTP Error 404: Not Found\\n'); sys.exit(1)\n"
        "sys.stdout.write(json.dumps(answers[key]))\n"
        f"sys.stderr.write({err!r}); sys.exit({code})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def setup(tmp_path, monkeypatch, *, session="r1-helper\n", parent="the-parent", **answers):
    sub_agent = load()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(sub_agent, "DIR", runs)
    if session is not None:
        (runs / "r1.session").write_text(session)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    kept = {"r1-helper": HELPER, "the-parent": PARENT, "stop": STOPPED}
    kept.update(answers)
    stub(bin_dir, "clarp-admin", calls, {k: v for k, v in kept.items() if v is not None})
    # The watcher of a helper that reported is gone already.
    stub(bin_dir, "systemctl", calls, {"--user": {}},
         err="Failed to stop clarp-sub-agent-r1.service: Unit clarp-sub-agent-r1.service "
             "not loaded.\n", code=5)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    if parent:
        monkeypatch.setenv("CLARP_SESSION", parent)
    else:
        monkeypatch.delenv("CLARP_SESSION", raising=False)
    return sub_agent, calls


def called(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def stops(calls):
    return [call for call in called(calls) if call[:2] == ["clarp-admin", "stop"]]


def units(calls):
    return [call for call in called(calls) if call[0] == "systemctl"]


def test_a_helper_is_stopped_through_the_host_then_its_watcher(tmp_path, monkeypatch, capsys):
    sub_agent, calls = setup(tmp_path, monkeypatch)
    assert sub_agent.main(["stop", "r1", "--reason", "wrong device"]) == 0
    assert stops(calls) == [["clarp-admin", "stop", "--session", "r1-helper", "--reason",
                             "wrong device", "--from", "the-parent"]]
    assert units(calls) == [["systemctl", "--user", "stop", "clarp-sub-agent-r1"]]
    out = capsys.readouterr().out
    assert "as agent:the-parent: its turn was stopped" in out


def test_a_helper_stop_says_why_when_no_reason_is_given(tmp_path, monkeypatch):
    sub_agent, calls = setup(tmp_path, monkeypatch)
    assert sub_agent.main(["stop", "r1"]) == 0
    stop = stops(calls)[0]
    assert stop[stop.index("--reason") + 1] == "stopped by its parent (clarp-sub-agent stop r1)"


def test_an_answer_that_does_not_say_the_queue_is_held_keeps_the_watcher(tmp_path, monkeypatch,
                                                                         capsys):
    # A name the Host does not have is answered ok with nothing held; an older shape says
    # nothing of the queue; a refusal is no Stop.
    for answer in ({"ok": True, "stop_actor": "agent:the-parent"},
                   {"ok": True, "queue_paused": False},
                   {"ok": False, "queue_paused": True},
                   {"ok": "true", "queue_paused": "true"}):
        case = tmp_path / str(len(list(tmp_path.iterdir())))
        case.mkdir()
        sub_agent, calls = setup(case, monkeypatch, stop=answer)
        assert sub_agent.main(["stop", "r1"]) == 1, answer
        assert len(stops(calls)) == 1 and units(calls) == [], answer
        assert "does not say its queue is held" in capsys.readouterr().err


def test_a_refused_stop_keeps_the_watcher(tmp_path, monkeypatch, capsys):
    sub_agent, calls = setup(tmp_path, monkeypatch, stop=None)
    assert sub_agent.main(["stop", "r1"]) == 1
    assert units(calls) == []
    assert "was not stopped" in capsys.readouterr().err


def test_a_name_that_maps_to_no_live_helper_of_the_caller_stops_nothing(tmp_path, monkeypatch):
    for case, kept in {
        "empty": {"session": "  \n"},
        "gone": {"r1-helper": None},
        "archived": {"r1-helper": {**HELPER, "archived_at": 1795700000000}},
        "not a helper": {"r1-helper": {**HELPER, "role": "agent"}},
        "another's": {"r1-helper": {**HELPER, "parent_agent_id": "p2"}},
    }.items():
        folder = tmp_path / case.replace(" ", "-").replace("'", "")
        folder.mkdir()
        sub_agent, calls = setup(folder, monkeypatch, **kept)
        assert sub_agent.main(["stop", "r1"]) == 1, case
        assert stops(calls) == [] and units(calls) == [], case


def test_a_helper_is_not_stopped_in_the_users_name(tmp_path, monkeypatch, capsys):
    # Without the caller's session the Host would record the Stop as the user's.
    sub_agent, calls = setup(tmp_path, monkeypatch, parent="")
    assert sub_agent.main(["stop", "r1"]) == 1
    assert called(calls) == []
    assert "CLARP_SESSION" in capsys.readouterr().err


def test_a_plain_worker_is_stopped_by_its_unit_alone(tmp_path, monkeypatch):
    sub_agent, calls = setup(tmp_path, monkeypatch, session=None)
    # Its unit's own answer is the command's, as before.
    assert sub_agent.main(["stop", "r1"]) == 5
    assert called(calls) == [["systemctl", "--user", "stop", "clarp-sub-agent-r1"]]


def test_a_watcher_ends_when_its_unreported_helper_is_archived(tmp_path, monkeypatch):
    row = {**HELPER, "helper_state": "ended-unreported"}
    sub_agent, calls = setup(tmp_path, monkeypatch, **{"r1-helper": row})
    (sub_agent.DIR / "r1.title").write_text("A helper\n")
    job_calls = tmp_path / "jobs.jsonl"
    stub(tmp_path / "bin", "clarp-agent-bg", job_calls, {"the-parent": "job-handle"})
    sleeps = []

    def archive(_seconds):
        assert not sleeps, "the watcher kept polling an archived helper"
        sleeps.append(True)
        stub(tmp_path / "bin", "clarp-admin", calls, {
            "r1-helper": {**row, "archived_at": 1795700000000},
        })

    assert sub_agent.watch("r1", "r1-helper", "the-parent", sleep=archive) == 1
    assert (sub_agent.DIR / "r1.exit").read_text() == "1\n"
    assert [call[2] for call in called(job_calls)] == [
        "job-upsert", "job-active", "job-heartbeat", "job-fail",
    ]
    assert called(job_calls)[-1][-1] == "helper r1-helper archived"
    assert stops(calls) == [] and units(calls) == []

"""tools/week-audit says when it could not read an agent whole, and never calls a gap a loss.

A Host on loopback answers the three GETs the audit makes from fixtures; the audit runs as it is
run by hand, and its exit and JSON report are read.
"""

import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

AUDIT = pathlib.Path(__file__).resolve().parents[1] / "week-audit"
NOW = int(time.time() * 1000)
SINCE = NOW - 3600 * 1000


def at(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((NOW - minutes_ago * 60000) / 1000))


def message(row_id: str, trace: str, minutes_ago: float) -> dict:
    return {"id": row_id, "role": "user", "origin": "user", "trace_id": trace, "text": "hello",
            "timestamp": at(minutes_ago)}


def answer(row_id: str, trace: str, minutes_ago: float) -> dict:
    ended = NOW - int(minutes_ago * 60000)
    return {"id": row_id, "role": "assistant", "phase": "final", "trace_id": trace, "text": "hi",
            "timestamp": at(minutes_ago),
            "turn": {"turn_id": trace, "status": "completed", "started_at_ms": ended - 5000, "ended_at_ms": ended}}


def log(*rows) -> tuple:
    return 200, {"turns": list(rows), "has_more": False, "conversation_id": "c"}


EMPTY_QUEUE = (200, {"items": [], "paused": False, "revision": 1, "steerable": False})
DOWN = (503, {"error": "unavailable"})


@pytest.fixture
def host():
    answers: dict[tuple[str, str], tuple] = {}

    class Fixture(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            session = urllib.parse.parse_qs(url.query).get("session", [""])[0]
            status, body = answers.get((url.path, session), (404, {"error": "agent not found"}))
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    answers[("/server-info", "")] = (200, {"name": "fixture", "version": "test"})
    yield server, answers
    server.shutdown()


def agents(answers, *sessions):
    answers[("/agents/snapshot", "")] = (200, {"agents": [
        {"session": s, "backend": "claude", "busy": False, "created_at": at(24 * 60)} for s in sessions]})


def audit(host, tmp_path, *args):
    server, _ = host
    out = tmp_path / "report.json"
    env = {**os.environ, "CLARP_HOST_TOKEN": "fixture", "HOME": str(tmp_path)}
    run = subprocess.run([sys.executable, str(AUDIT), "--host", f"127.0.0.1:{server.server_address[1]}",
                          "--pause", "0", "--since", str(SINCE), "--out", str(out), *args],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.exists(), run.stderr
    return run, json.loads(out.read_text())


def test_an_agent_whose_log_is_not_read_stays_in_a_fleet_report_and_fails_it(host, tmp_path):
    _, answers = host
    agents(answers, "quiet", "unread")
    answers[("/log", "quiet")] = log()
    answers[("/turn-queue", "quiet")] = EMPTY_QUEUE
    answers[("/log", "unread")] = DOWN

    run, report = audit(host, tmp_path)

    assert run.returncode == 4, run.stdout
    assert report["agents"]["unread"]["gaps"] == [
        {"source": "log", "status": 503, "problem": "HTTP 503: unavailable", "pages": 0}]
    coverage = report["coverage"]
    assert (coverage["agents_requested"], coverage["agents_read_whole"], coverage["agents_incomplete"]) == (2, 1, 1)
    assert not coverage["complete"]
    assert "COVERAGE INCOMPLETE" in run.stdout and "gap: unread log HTTP 503" in run.stdout


@pytest.mark.parametrize("queue, code, lost, unconfirmed", [
    (DOWN, 4, 0, 1),  # the queue unread: the message may wait there, so it is a gap
    (EMPTY_QUEUE, 1, 1, 0),  # the queue read and empty: the message is lost
])
def test_a_message_is_lost_only_when_the_queue_was_read(host, tmp_path, queue, code, lost, unconfirmed):
    _, answers = host
    agents(answers, "a")
    answers[("/log", "a")] = log(message("u-1", "t-1", 20))
    answers[("/turn-queue", "a")] = queue

    run, report = audit(host, tmp_path, "--session", "a")

    assert run.returncode == code, run.stdout
    totals = report["totals"]
    assert (totals["lost"], totals["unconfirmed"]) == (lost, unconfirmed)
    sources = [found["source"] for found in report["coverage"]["gaps"]]
    assert sources == (["turn-queue"] if queue is DOWN else [])


def test_a_whole_read_with_nothing_found_passes_with_its_counts(host, tmp_path):
    _, answers = host
    agents(answers, "a")
    answers[("/log", "a")] = log(message("u-1", "t-1", 20), answer("a-1", "t-1", 19))
    answers[("/turn-queue", "a")] = EMPTY_QUEUE

    run, report = audit(host, tmp_path)

    assert run.returncode == 0, run.stdout
    totals = report["totals"]
    assert (totals["agents"], totals["messages"], totals["turns"]) == (1, 1, 1)
    assert totals["turn_status"] == {"completed": 1}
    assert (totals["lost"], totals["doubled"], totals["stuck"], totals["unconfirmed"]) == (0, 0, 0, 0)
    assert report["coverage"] == {"agents_requested": 1, "agents_read_whole": 1, "agents_incomplete": 0,
                                  "complete": True, "gaps": []}
    assert "COVERAGE complete: 1 of 1" in run.stdout
    assert "#933" in report["evidence"]

"""TP-N3 (ii) / BACKENDS-01: a Codex turn/start in flight must not hold up
other agents, Stop, or the agent's own follow-up.

rachel's turn/start is held (only her request blocks; the app-server, the
pool and every other agent are real, against tests/qa/fake_codex.py). Before
the fix the pool lock was held across the RPC and the dispatcher waited for
it inside _TURN_LOCK, so one hung start stalled every send and Stop."""
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from lib import agents as agents_db, backends, codex_app_server as cas, turn_dispatch as td
from lib.backend.registry import by_id

FAKE = Path(__file__).resolve().parents[2] / "tests/qa/fake_codex.py"


class _Stream:
    def broadcast(self, event):
        pass


def _agent(tmp_path, name):
    agent_id = agents_db.create_agent(persona=name.title(), voice_id="v", cwd=str(tmp_path),
                                      session=name, backend="codex")
    agents_db.start_runtime(agent_id, name)
    return agent_id


def _timed(fn, timeout):
    """Run ``fn`` in a thread; (finished_in_time, result_or_exception, seconds)."""
    box = {}

    def run():
        t0 = time.monotonic()
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["value"] = exc
        box["seconds"] = time.monotonic() - t0

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return (not thread.is_alive()), box.get("value"), box.get("seconds"), thread


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    monkeypatch.setattr(by_id("codex"), "required_binary", str(FAKE))
    cas._CLIENTS.clear()
    td.reset_for_tests()
    service = td.TurnDispatchService(SimpleNamespace(default_session="rachel", stream=_Stream(),
                                                     agents_path=tmp_path / "unused.json"),
                                     backend_registry=backends, home=tmp_path)
    held, release, sent = threading.Event(), threading.Event(), []
    real = cas._Client.request

    def request(self, method, params, timeout=30, **kwargs):
        sent.append((method, dict(params)))
        if method == "turn/start" and "HOLD" in str(params.get("input")):
            held.set()
            if not release.wait(timeout):
                raise TimeoutError("Codex app-server timed out: turn/start")
        return real(self, method, params, timeout=timeout, **kwargs)

    monkeypatch.setattr(cas._Client, "request", request)
    world = SimpleNamespace(service=service, held=held, release=release, sent=sent,
                            rachel=_agent(tmp_path, "rachel"), mike=_agent(tmp_path, "mike"))
    yield world
    release.set()
    # As test_codex_start_guardrails: an idle moment later, and a recycled
    # process that has exited, or the next test's quiescence gate fails.
    from tests.codex_pool_lifecycle import retire_left_clients
    retire_left_clients()


def _hold_rachel(world):
    done, _, _, thread = _timed(lambda: world.service.dispatch(
        text="HOLD please", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert world.held.wait(15), "control: rachel's turn/start is held"
    return thread


def test_other_agents_and_stop_do_not_wait_for_a_held_start(world):
    _hold_rachel(world)
    ok, result, seconds, _ = _timed(lambda: world.service.dispatch(
        text="hello", requested_session="mike", trace_id="t-m1", synthesize_audio=False), 3.0)
    assert ok, "mike's send waited for rachel's turn/start"
    assert not isinstance(result, Exception), result
    ok, _, _, _ = _timed(lambda: td.snapshot_stop_state(world.rachel), 3.0)
    assert ok, "the Stop's snapshot waited for rachel's turn/start"
    ok, _, _, _ = _timed(lambda: td.snapshot_stop_state(world.mike), 3.0)
    assert ok


def test_a_stop_during_a_held_start_interrupts_that_turn_by_id_without_killing_the_server(world):
    starter = _hold_rachel(world)
    client = cas._CLIENTS[cas._SHARED_KEY]
    ok, _, _, _ = _timed(lambda: (td.clear_for_agent(world.rachel, preserve_queue=True, pause_queue=True),
                                  backends.interrupt("codex", world.rachel)), 3.0)
    assert ok, "the Stop waited for the held start"
    assert client.proc.poll() is None, "a Stop during a start killed the shared app-server"
    world.release.set()
    starter.join(15)
    started = [p for m, p in world.sent if m == "turn/start" and "HOLD" in str(p.get("input"))]
    assert started
    assert _wait(lambda: any(m == "turn/interrupt" for m, _ in world.sent)), \
        "the stopped turn was not interrupted once its start returned"
    assert client.proc.poll() is None


def test_the_agents_own_follow_up_during_its_held_start_queues_at_once(world):
    _hold_rachel(world)
    ok, result, seconds, _ = _timed(lambda: world.service.dispatch(
        text="and also", requested_session="rachel", trace_id="t-r2", synthesize_audio=False), 3.0)
    assert ok, "rachel's follow-up waited for her own held start"
    assert getattr(result, "queued", False), f"the follow-up was not queued: {result}"
    world.release.set()
    assert _wait(lambda: any(m == "turn/start" and "and also" in str(p.get("input"))
                             for m, p in world.sent), 20), "the queued follow-up never ran"


def test_a_start_that_never_returns_fails_on_its_timeout_and_blocked_no_one(world, monkeypatch):
    monkeypatch.setattr(cas, "START_RPC_TIMEOUT_SEC", 1.0, raising=False)
    starter = _hold_rachel(world)
    ok, _, _, _ = _timed(lambda: world.service.dispatch(
        text="hello", requested_session="mike", trace_id="t-m1", synthesize_audio=False), 3.0)
    assert ok
    starter.join(10)
    assert not starter.is_alive(), "the held start never timed out"

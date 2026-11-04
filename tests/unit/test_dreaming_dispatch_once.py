"""AUT-11: a dream round is dispatched at most once and an agent has at most one
active dream run, even when two schedulers (two Host processes, or the loop and
a wake thread) advance the same run at the same moment."""
from __future__ import annotations

import threading
import urllib.error

import pytest

from lib import agents as agents_db
from lib import dream_seeds, dreaming


@pytest.fixture(autouse=True)
def wikipedia_unreachable(monkeypatch):
    real_urlopen = dream_seeds.urllib.request.urlopen

    def urlopen(request, *args, **kwargs):
        if "wikipedia.org" in str(getattr(request, "full_url", request)):
            raise urllib.error.URLError("wikipedia is unreachable in tests")
        return real_urlopen(request, *args, **kwargs)
    monkeypatch.setattr(dream_seeds.urllib.request, "urlopen", urlopen)


def _run(session="domi"):
    aid = agents_db.create_agent(persona=session.capitalize(), voice_id="v", cwd="/tmp", session=session)
    agents_db.update_agent(aid, dreaming_enabled=True)
    agent = agents_db.get_by_agent_id(aid)
    run = dreaming.create_dream_run(agent, local_date="2026-06-24", timezone_name="UTC",
                                    timezone_source="test")
    return aid, run


def _round(run_id):
    return dict(dreaming.db.conn().execute(
        "SELECT round_id, status, sent_at FROM dream_rounds WHERE run_id=? ORDER BY round_index LIMIT 1",
        (run_id,)).fetchone())


def _active_runs(agent_id):
    return dreaming.db.conn().execute(
        "SELECT COUNT(*) FROM dream_runs WHERE agent_id=? AND status='active'", (agent_id,)).fetchone()[0]


def test_two_schedulers_advancing_one_run_at_once_send_its_round_once():
    _aid, run = _run()
    sending, release = threading.Event(), threading.Event()
    sends = []

    def slow_send(session, prompt):
        sends.append(session)
        sending.set()
        release.wait(5.0)
        return True

    first = dreaming.DreamingScheduler(send_dream=slow_send)
    second = dreaming.DreamingScheduler(send_dream=lambda s, p: sends.append(s) or True)
    worker = threading.Thread(target=first._advance_run, args=(run,))
    worker.start()
    try:
        assert sending.wait(5.0)
        assert second._advance_run(run) == 0, "the round is already being sent"
    finally:
        release.set()
        worker.join(5.0)
    assert sends == ["domi"]
    assert _round(run["run_id"])["status"] == "sent"


@pytest.mark.parametrize("failure", ["raises", "busy"])
def test_a_send_that_fails_puts_the_round_back_for_a_later_tick(failure):
    _aid, run = _run()

    def send(session, prompt):
        if failure == "raises":
            raise RuntimeError("dispatch failed")
        return False
    assert dreaming.DreamingScheduler(send_dream=send)._advance_run(run) == 0
    assert _round(run["run_id"]) | {"round_id": None} == {"round_id": None, "status": "queued", "sent_at": None}
    sends = []
    assert dreaming.DreamingScheduler(send_dream=lambda s, p: sends.append(s) or True)._advance_run(run) == 1
    assert sends == ["domi"]


def test_marking_a_round_sent_leaves_an_abandoned_round_abandoned():
    _aid, run = _run()
    round_id = _round(run["run_id"])["round_id"]
    dreaming.db.conn().execute("UPDATE dream_rounds SET status='abandoned' WHERE round_id=?", (round_id,))
    dreaming.mark_round_sent(round_id)
    assert _round(run["run_id"])["status"] == "abandoned"


def test_an_agent_gets_no_second_active_run():
    aid, _run_row = _run()
    agent = agents_db.get_by_agent_id(aid)
    with pytest.raises(dreaming.ActiveDreamRunExists):
        dreaming.create_dream_run(agent, local_date="2026-06-24", timezone_name="UTC",
                                  timezone_source="test")
    assert _active_runs(aid) == 1


def test_the_nightly_loop_skips_an_agent_whose_manual_run_started_meanwhile(monkeypatch):
    aid, _run_row = _run()
    agent = agents_db.get_by_agent_id(aid)
    # The loop listed the agent before the manual start created its run.
    monkeypatch.setattr(dreaming, "pending_dreaming_agents", lambda now=None: [{
        **agent, "dreaming_local_date": "2026-06-24", "dreaming_timezone": "UTC",
        "dreaming_timezone_source": "test", "dreaming_settings": dreaming.get_settings()}])
    monkeypatch.setattr(dreaming, "active_runs", lambda: [])
    sends = []
    dreaming.DreamingScheduler(send_dream=lambda s, p: sends.append(s) or True).run_once()
    assert _active_runs(aid) == 1
    assert sends == []

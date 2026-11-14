"""Turns parked for a model start when the account in use serves it again.

elitebook, 2026-11-14: every saved account's Fable quota was spent at 23:19.
The pool's recovery moved to an account without Fable, resumed the Opus turns
and parked the Fable ones. At 23:31:30 the Hotseat switcher moved the machine
to an account with Fable. Nothing told the runtime: it does not ask the
selector while other turns work, so seven Fable turns (the owner's messages
among them) stayed parked until 23:47:33, when the one Opus turn that kept the
pool busy was stopped by hand. Their own cap would have been 23:49.

Now the switcher's check tells the runtime which wanted model families the
account in use has come to serve, and the turns parked for them start.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import janitor_autonomy as service
from lib import janitor_hotseat as hotseat
from lib.account_failover import MAX_PARK_SECONDS
from lib.runtime_bridge import RuntimeClient
from tests.unit.test_account_failover_followup import (  # noqa: F401
    MINUTE, WEEKLY, _fable_parked_beside_sonnet, _started)
from tests.unit.test_account_plan_hostage import (  # noqa: F401
    Fleet, _dispatch, _local_dispatch_runtime)
from tests.unit.test_janitor_hotseat import claude_payload, switcher  # noqa: F401
from tests.unit.test_runtime_dispatch_characterization import rpc  # noqa: F401
from tests.unit.test_dispatch_adapters import adapters  # noqa: F401


# --- the pool's coordinator -------------------------------------------------

def test_a_parked_turn_starts_when_the_account_is_said_to_serve_its_model(tmp_path, monkeypatch):
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    sonnet_processes, asked = len(fleet.spawns(1)), len(fleet.selector_calls())
    fleet.run_until(start + 12 * MINUTE)

    assert fleet.coordinator.served_now(lambda model: model == "fable") == 1

    # It started then, not at its half hour and not when Sonnet went idle.
    assert fleet.starts(0)[1:] == [start + 12 * MINUTE]
    assert fleet.waiting() == [None, None]
    # Sonnet was not stopped for it and the selector was not run.
    assert len(fleet.spawns(1)) == sonnet_processes and len(fleet.selector_calls()) == asked
    assert not fleet.coordinator.recovering
    # With nothing parked for Fable any more, a new Fable turn runs at once.
    _dispatch(fleet, "fable-new", "fable")
    assert _started(fleet, "fable-new") == [start + 12 * MINUTE]
    # The timer armed for the parked turn finds nothing to do.
    fleet.run_until(start + MAX_PARK_SECONDS + 5 * MINUTE)
    assert fleet.starts(0)[1:] == [start + 12 * MINUTE] and fleet.scheduled == []


def test_every_turn_parked_for_the_model_starts_and_the_others_stay(tmp_path, monkeypatch):
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 2 * MINUTE)
    _dispatch(fleet, "fable-behind", "fable")          # parks behind the first one
    assert _started(fleet, "fable-behind") == []

    assert fleet.coordinator.served_now(lambda model: model == "opus") == 0
    assert fleet.starts(0)[1:] == [] and _started(fleet, "fable-behind") == []

    fleet.run_until(start + 3 * MINUTE)
    assert fleet.coordinator.served_now(lambda model: model == "fable") == 2
    assert fleet.starts(0)[1:] == [start + 3 * MINUTE]
    assert _started(fleet, "fable-behind") == [start + 3 * MINUTE]


def test_during_a_recovery_the_selector_decides_not_the_report(tmp_path, monkeypatch):
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["fable", "sonnet"],
                  unserved=("fable", "sonnet"))
    fleet.limit(0, WEEKLY)
    fleet.run_scheduled()
    assert fleet.coordinator.recovering and fleet.waiting() == ["waiting", "waiting"]
    spawned = len(fleet.backends.spawned)

    assert fleet.coordinator.served_now(lambda model: True) == 0

    assert fleet.coordinator.recovering and len(fleet.backends.spawned) == spawned


def test_a_turn_started_on_a_wrong_report_is_a_relapse(tmp_path, monkeypatch):
    """The report was wrong: the turn it started meets the limit again. That
    is handled as after a recovery whose answer did not hold, and the report
    is not what starts it a second time."""
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 12 * MINUTE)
    assert fleet.coordinator.served_now(lambda model: model == "fable") == 1
    key = fleet.key(0)
    assert fleet.coordinator.resumed_at[key] == start + 12 * MINUTE
    assert fleet.coordinator.last_resumed == start + 12 * MINUTE

    fleet.limit(0, WEEKLY)

    assert fleet.coordinator._relapsed(key) and fleet.coordinator.relapses == 1


# --- the runtime's side of the call -----------------------------------------

def test_the_runtime_starts_the_turns_of_the_named_family(tmp_path, monkeypatch):
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["claude-fable-5-2", "claude-sonnet-5-5"],
                  unserved=("claude-fable-5-2",))
    start = fleet.clock[0]
    fleet.limit(0, WEEKLY)
    fleet.run_until(start)
    assert fleet.waiting() == ["waiting", None]

    assert service.runtime_seat_serves("claude", ["opus"]) == {"status": "nothing_parked", "started": 0}
    assert service.runtime_seat_serves("claude", []) == {"status": "nothing_parked", "started": 0}
    assert fleet.waiting() == ["waiting", None]

    assert service.runtime_seat_serves("claude", ["fable"]) == {"status": "started", "started": 1}
    assert fleet.waiting() == [None, None]


def test_the_runtime_refuses_a_provider_without_model_quotas():
    assert service.runtime_seat_serves("codex", ["fable"]) == {"status": "unsupported"}
    assert service.runtime_seat_serves("nobody", ["fable"]) == {"status": "unsupported"}


def test_the_runtime_answers_the_call(rpc, monkeypatch):
    serves = Mock(return_value={"status": "started", "started": 2})
    monkeypatch.setattr(service, "runtime_seat_serves", serves)
    assert rpc.dispatch_request("seat_serves", {"provider": "claude", "families": ["fable"]}) == {
        "ok": True, "result": {"status": "started", "started": 2}}
    serves.assert_called_once_with("claude", ["fable"])
    rpc.dispatch_request("seat_serves", {"provider": "claude", "families": "fable"})
    assert serves.call_args.args == ("claude", [])


def test_a_runtime_from_before_the_call_is_reported_as_unavailable():
    client = object.__new__(RuntimeClient)
    sent = []

    def request(method, params):
        sent.append((method, params))
        return {"ok": False, "status": 404, "error": "unknown runtime method: seat_serves"}
    client._request = request
    assert client.seat_serves("claude", ["fable"]) == {"status": "runtime_unavailable"}
    assert sent == [("seat_serves", {"provider": "claude", "families": ["fable"]})]
    client._request = lambda method, params: {"ok": True, "result": {"status": "started", "started": 1}}
    assert client.seat_serves("claude", ["fable"]) == {"status": "started", "started": 1}


def test_the_host_sends_it_to_the_runtime_when_there_is_one(adapters, monkeypatch):
    calls = []
    adapters.ctx.runtime_client = SimpleNamespace(
        seat_serves=lambda *args: calls.append(("runtime", args)) or "ok")
    assert adapters.seat_serves("claude", ["fable"]) == "ok"
    adapters.ctx.runtime_client = None
    monkeypatch.setattr(service, "runtime_seat_serves",
                        lambda *args: calls.append(("local", args)) or "local-ok")
    assert adapters.seat_serves("claude", ["fable"]) == "local-ok"
    assert calls == [("runtime", ("claude", ["fable"])), ("local", ("claude", ["fable"]))]


# --- the switcher's check ---------------------------------------------------

def _fable(payload, **used):
    """Give accounts a Fable week: alias -> the share of it that is used."""
    for account in payload["accounts"]:
        if account["alias"] in used:
            account["usage"]["scoped"] = [{"model": "Fable", "used": used[account["alias"]]}]
    return payload


@pytest.fixture
def told(switcher, monkeypatch):  # noqa: F811
    owner, worker, readings, calls, sent = switcher
    monkeypatch.setattr(service, "_provider_model_demand",
                        lambda *a: (("claude-fable-5-2",), ("claude-opus-5-5",)))
    heard = []
    answer = {"status": "started", "started": 3}

    def seat_serves(provider, families):
        heard.append((provider, list(families)))
        return dict(answer)
    worker.seat_serves = seat_serves

    def check():
        from tests.unit.test_janitor_hotseat import _check_again
        _check_again("hotseat-switcher.claude")
        worker.hotseat_once()
    return SimpleNamespace(worker=worker, readings=readings, calls=calls, heard=heard,
                           answer=answer, check=check)


def test_a_switch_to_an_account_with_the_model_is_told_to_the_runtime(told):
    # a is in use and has no Fable left; b has.
    told.readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=1.0, b=0.4)
    told.check()
    assert ("switch", "hotseat", "claude", "b") in told.calls
    assert told.heard == [("claude", ["fable"])]

    # The next check, on b: nothing new to tell.
    told.readings["claude"] = _fable(claude_payload(a=(False, 0.2), b=(True, 0.1)), a=1.0, b=0.4)
    told.check()
    assert told.heard == [("claude", ["fable"])]


def test_an_account_that_stays_without_the_model_tells_nothing_until_it_has_it(told):
    told.readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=1.0, b=1.0)
    told.check()
    assert told.heard == [] and not [c for c in told.calls if c[0] == "switch"]

    # The week's reset arrives on the account in use (or a banked one is redeemed).
    told.readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=0.0, b=1.0)
    told.check()
    assert told.heard == [("claude", ["fable"])]
    told.check()
    assert told.heard == [("claude", ["fable"])]


def test_a_runtime_that_cannot_hear_it_yet_is_told_at_the_next_check(told):
    told.readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=0.3, b=1.0)
    told.answer.clear()
    told.answer.update(status="runtime_unavailable")
    told.check()
    told.check()
    assert told.heard == [("claude", ["fable"])] * 2

    told.answer.update(status="nothing_parked", started=0)
    told.check()
    told.check()
    assert told.heard == [("claude", ["fable"])] * 3


def test_a_call_that_raises_does_not_fail_the_check(told):
    told.readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=0.3, b=1.0)

    def broken(provider, families):
        told.heard.append("raised")
        raise OSError("runtime socket gone")
    told.worker.seat_serves = broken
    told.check()
    told.check()
    assert told.heard == ["raised", "raised"]


def test_the_switcher_works_without_a_runtime_to_tell(switcher, monkeypatch):  # noqa: F811
    owner, worker, readings, calls, sent = switcher
    monkeypatch.setattr(service, "_provider_model_demand",
                        lambda *a: (("claude-fable-5-2",), ()))
    readings["claude"] = _fable(claude_payload(a=(True, 0.2), b=(False, 0.1)), a=1.0, b=0.4)
    assert worker.seat_serves is None
    worker.hotseat_once()
    assert ("switch", "hotseat", "claude", "b") in calls


def test_the_server_gives_the_switcher_the_call():
    import pathlib
    source = (pathlib.Path(service.__file__).parents[1] / "server.py").read_text()
    assert "seat_serves=adapters.seat_serves" in source
    assert hotseat.PROVIDERS["claude"].model_scoped and not hotseat.PROVIDERS["codex"].model_scoped

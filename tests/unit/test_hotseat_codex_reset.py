"""The Hotseat keeper redeems a banked Codex reset when no saved Codex account has room (R145).

The reset lane was Claude's alone: Codex had no reset command, and a receipt's account had to look
like a Claude account name. Now each provider's receipt is checked against that provider's saved
aliases, as the keeper's own read gives them; Codex has codex_reset_command; and the Host caps
verified automatic Codex redemptions at codex_reset_max_per_day in a rolling 24 hours (default 1,
0 switches the step off), kept in the settings store so a restart does not forget them. The owner
is told each time one is spent, and once when the cap or an empty bank stops the step.

Every broker here is a stand-in script that answers the receipt protocol. Nothing is redeemed."""
from __future__ import annotations

import dataclasses
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib import config, db, janitor_reset, settings_store
from tests.unit.test_janitor_hotseat import _check_again, switcher  # noqa: F401 - the keeper's fixture


def _broker(tmp_path, monkeypatch, *, cap=1, sleep=0.0):
    """A stand-in broker: it records each request it is given and answers the receipt in a file."""
    script = tmp_path / "broker.py"
    receipt = tmp_path / "receipt.json"
    requests = tmp_path / "requests.jsonl"
    script.write_text(
        "import json,pathlib,sys,time\n"
        f"time.sleep({sleep})\n"
        "pathlib.Path(sys.argv[2]).open('a').write(sys.stdin.read().strip()+'\\n')\n"
        "print(pathlib.Path(sys.argv[1]).read_text())\n")
    cfg = config.load()
    monkeypatch.setattr(config, "load", lambda: dataclasses.replace(
        cfg, codex_reset_command=(sys.executable, str(script), str(receipt), str(requests)),
        codex_reset_max_per_day=cap))
    return receipt, requests


def _requests(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _codex(*accounts, five_hour=None):
    """Hotseat's Codex reading: each account's own weekly window (used = 1 - room) and, when given,
    its five-hour window; worst_used and blocked are the worse of the two, as Hotseat gives them."""
    def usage(room):
        windows = [{"label": "codex primary weekly", "used": 1 - room}]
        if five_hour is not None:
            windows.append({"label": "codex primary 5-hour", "used": five_hour})
        worst = max(w["used"] for w in windows)
        return {"usable": worst < 1, "blocked": worst >= 1, "error": None, "worst_used": worst, "windows": windows}
    return {"accounts": [{"alias": alias, "is_active": active, "usage": usage(room)}
                         for alias, active, room in accounts]}


# ---- the receipt, per provider ------------------------------------------------------------------

@pytest.mark.parametrize("receipt,saved,verified", [
    ({"status": "redeemed", "verified": True, "account": "work", "resets_left": 2}, ("work", "home"), True),
    ({"status": "redeemed", "verified": True, "account": "elsewhere"}, ("work", "home"), False),
    ({"status": "redeemed", "verified": True, "account": "ai2"}, ("work", "home"), False),   # a Claude-shaped name
    ({"status": "redeemed", "verified": True, "account": "work"}, (), False),                # no saved names given
    ({"status": "no_eligible_grant", "verified": False}, ("work",), False),
])
def test_a_codex_receipt_is_verified_only_for_a_saved_codex_alias(receipt, saved, verified):
    class Process:
        pid = 123
        returncode = 0
        def communicate(self, **kwargs):
            return json.dumps(receipt), ""
    with patch.object(janitor_reset.config, "load", return_value=SimpleNamespace(codex_reset_command=("broker",))), \
            patch.object(janitor_reset.subprocess, "Popen", return_value=Process()), \
            patch.object(janitor_reset, "_kill_group"):
        result = janitor_reset.attempt("codex_reset_command", [], accounts=saved, provider="codex")
    assert result["verified"] is verified
    assert result.get("resets_left") == (2 if verified else None)


def test_a_failing_codex_broker_is_logged_under_codex(monkeypatch):
    faults = []
    monkeypatch.setattr(janitor_reset, "log_fault", lambda name, exc: faults.append(name))
    with patch.object(janitor_reset.config, "load", return_value=SimpleNamespace(codex_reset_command=("/no/such/broker",))):
        result = janitor_reset.attempt("codex_reset_command", [], accounts=("work",), provider="codex")
    assert result["status"] == "unverified" and faults == ["codexBankedResetCommandFailed"]


# ---- the cap, kept across a restart --------------------------------------------------------------

def _owner():
    return {"agent_id": "keeper", "generation": 1, "options": {"mode": "automatic"}}


@pytest.fixture
def keeper_owns(monkeypatch):
    """The worker cancels an attempt whose owner is no longer the Hotseat keeper's current one;
    the worker-level tests below name that owner."""
    from lib import janitor_builtins, turn_queue
    real_resolve = janitor_builtins.resolve
    monkeypatch.setattr(janitor_builtins, "resolve",
                        lambda role: _owner() if role == "account-hotseat" and _owned[0] else real_resolve(role))
    real_generation = turn_queue.user_stop_generation
    monkeypatch.setattr(turn_queue, "user_stop_generation",
                        lambda agent_id: 0 if agent_id == "keeper" else real_generation(agent_id))
    _owned[0] = True
    yield
    _owned[0] = False


_owned = [False]


def _run(worker):
    if worker.thread:
        worker.thread.join(5)
        assert not worker.thread.is_alive()


def test_one_verified_redemption_a_day_and_a_restart_does_not_forget_it(tmp_path, monkeypatch, keeper_owns):
    receipt, requests = _broker(tmp_path, monkeypatch)
    receipt.write_text(json.dumps({"status": "redeemed", "verified": True, "account": "work"}))
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    worker = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    assert worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                        cap_field="codex_reset_max_per_day") == "checking"
    _run(worker)
    assert _requests(requests) == [{"models": []}]
    clock[0] += 11 * 60_000                                              # past the lane's own interval
    restarted = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    assert restarted.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                           cap_field="codex_reset_max_per_day") == "daily_cap"
    assert len(_requests(requests)) == 1                                 # the broker was not called
    clock[0] += 24 * 60 * 60_000
    assert restarted.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                           cap_field="codex_reset_max_per_day") == "checking"
    _run(restarted)
    assert len(_requests(requests)) == 2


def test_an_unverified_redemption_does_not_count_against_the_cap(tmp_path, monkeypatch, keeper_owns):
    receipt, requests = _broker(tmp_path, monkeypatch)
    receipt.write_text(json.dumps({"status": "redeemed", "verified": True, "account": "not-saved"}))
    worker = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",), cap_field="codex_reset_max_per_day")
    _run(worker)
    assert worker.spent_today("codex") == [] and worker.capped("codex", "codex_reset_max_per_day") == ""


def test_a_cap_of_nought_switches_the_step_off(tmp_path, monkeypatch, keeper_owns):
    receipt, requests = _broker(tmp_path, monkeypatch, cap=0)
    worker = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    assert worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                        cap_field="codex_reset_max_per_day") == "disabled"
    assert _requests(requests) == []


def test_never_two_in_flight(tmp_path, monkeypatch, keeper_owns):
    receipt, requests = _broker(tmp_path, monkeypatch, sleep=1.0)
    receipt.write_text(json.dumps({"status": "no_eligible_grant", "verified": False}))
    worker = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    assert worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                        cap_field="codex_reset_max_per_day") == "checking"
    assert worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",),
                        cap_field="codex_reset_max_per_day") == "busy"
    _run(worker)
    assert len(_requests(requests)) == 1


def test_an_unverified_attempt_is_reconciled_before_another_is_offered(tmp_path, monkeypatch, keeper_owns):
    receipt, requests = _broker(tmp_path, monkeypatch)
    receipt.write_text(json.dumps({"status": "unverified", "verified": False}))
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    worker = janitor_reset.ResetWorker(SimpleNamespace(is_set=lambda: False))
    worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",), cap_field="codex_reset_max_per_day")
    _run(worker)
    clock[0] += 11 * 60_000
    receipt.write_text(json.dumps({"status": "reconciled", "verified": True, "account": "work"}))
    worker.offer("codex", "codex_reset_command", [], _owner(), accounts=("work",), cap_field="codex_reset_max_per_day")
    _run(worker)
    assert _requests(requests) == [{"models": []}, {"models": [], "reconcile_only": True}]
    state = json.loads(settings_store.get_text("hotseat-reset.codex.state"))
    assert state["result"]["verified"] is True and state["accounts"] == ["work"]


# ---- the keeper -----------------------------------------------------------------------------------

def _keeper_run(worker, clock):
    _check_again("hotseat-switcher.codex")
    worker.hotseat_once()
    if worker.reset_worker.thread:
        worker.reset_worker.thread.join(5)
    clock[0] += 1
    worker.hotseat_once()                                                # delivers a completion, if any


def test_the_keeper_offers_a_codex_reset_only_when_no_saved_account_has_room(switcher, tmp_path, monkeypatch):  # noqa: F811
    owner, worker, readings, calls, sent = switcher
    receipt, requests = _broker(tmp_path, monkeypatch)
    receipt.write_text(json.dumps({"status": "redeemed", "verified": True, "account": "work", "resets_left": 1}))
    worker.reset_worker = janitor_reset.ResetWorker(worker.stop_event)
    clock = [db.now_ms() + 1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    readings["codex"] = _codex(("work", True, 0.0), ("home", False, 0.9))     # another account has room
    _keeper_run(worker, clock)
    assert _requests(requests) == [] and ("switch", "hotseat", "codex", "home") in [c[:4] for c in calls]
    readings["codex"] = _codex(("work", True, 0.0), ("home", False, 0.0))     # none has room
    clock[0] += 20 * 60_000
    _keeper_run(worker, clock)
    assert _requests(requests) == [{"models": []}]
    told = [n["preview"] for n in sent if n["preview"].startswith("Banked reset ")]
    assert told == ["Banked reset redeemed for work; quota refill verified. 1 banked reset left on it."]


@pytest.mark.parametrize("stop,receipt_body,cap", [
    ("daily_cap", {"status": "redeemed", "verified": True, "account": "work"}, 1),
    ("empty", {"status": "no_eligible_grant", "verified": False}, 5),
])
def test_what_stops_the_step_is_told_once(switcher, tmp_path, monkeypatch, stop, receipt_body, cap):  # noqa: F811
    owner, worker, readings, calls, sent = switcher
    receipt, requests = _broker(tmp_path, monkeypatch, cap=cap)
    receipt.write_text(json.dumps(receipt_body))
    worker.reset_worker = janitor_reset.ResetWorker(worker.stop_event)
    clock = [db.now_ms() + 1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    readings["codex"] = _codex(("work", True, 0.0), ("home", False, 0.0))
    for _ in range(5):
        _keeper_run(worker, clock)
        clock[0] += 20 * 60_000
    wanted = {"daily_cap": "Codex: no automatic banked reset now: the cap of 1 in 24 hours is reached.",
              "empty": "Codex: no banked reset is left to redeem."}[stop]
    assert [n["preview"] for n in sent].count(wanted) == 1
    if stop == "daily_cap":
        assert len(_requests(requests)) == 1                              # one redemption, then the cap


def test_claude_receipts_are_checked_against_claudes_own_saved_names(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        def communicate(self, **kwargs):
            return json.dumps({"status": "redeemed", "verified": True, "account": "ai2"}), ""
    with patch.object(janitor_reset.config, "load", return_value=SimpleNamespace(claude_reset_command=("broker",))), \
            patch.object(janitor_reset.subprocess, "Popen", return_value=Process()), \
            patch.object(janitor_reset, "_kill_group"):
        assert janitor_reset.attempt("claude_reset_command", ["Fable"], accounts=("ai2", "ai9"))["verified"] is True
        assert janitor_reset.attempt("claude_reset_command", ["Fable"], accounts=("ai9",))["verified"] is False


@pytest.mark.parametrize("status", ["not_applicable", "cooldown", "busy", "no_owned_reset_accounts", "unverified"])
def test_a_try_later_answer_tells_the_owner_nothing(switcher, tmp_path, monkeypatch, status):  # noqa: F811
    """The broker's answers that are not an empty bank (an account has room, it checked lately or a
    holder's window resets soon, its lock is held, no saved profile, an unverified write): no notice."""
    owner, worker, readings, calls, sent = switcher
    receipt, requests = _broker(tmp_path, monkeypatch, cap=5)
    receipt.write_text(json.dumps({"status": status, "verified": False}))
    worker.reset_worker = janitor_reset.ResetWorker(worker.stop_event)
    clock = [db.now_ms() + 1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    readings["codex"] = _codex(("work", True, 0.0), ("home", False, 0.0))
    for _ in range(3):
        _keeper_run(worker, clock)
        clock[0] += 20 * 60_000
    assert len(_requests(requests)) >= 1
    assert not [n for n in sent if "banked reset" in n["preview"].lower()]


# ---- when the Host calls the broker (F1, F2) -----------------------------------------------------

def _keeper(switcher, tmp_path, monkeypatch, reading):
    owner, worker, readings, calls, sent = switcher
    receipt, requests = _broker(tmp_path, monkeypatch, cap=5)
    receipt.write_text(json.dumps({"status": "no_eligible_grant", "verified": False}))
    worker.reset_worker = janitor_reset.ResetWorker(worker.stop_event)
    clock = [db.now_ms() + 1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    readings["codex"] = reading
    _keeper_run(worker, clock)
    return _requests(requests)


@pytest.mark.parametrize("reading,called", [
    # every account at its FIVE-HOUR cap, with weekly room: a banked (weekly) reset is not for that
    (_codex(("work", True, 0.5), ("home", False, 0.7), five_hour=1.0), False),
    # one account still has weekly room
    (_codex(("work", True, 0.0), ("home", False, 0.2)), False),
    # every account's weekly window spent: one call
    (_codex(("work", True, 0.0), ("home", False, 0.0)), True),
])
def test_the_host_asks_for_a_banked_reset_only_when_every_weekly_window_is_spent(
        switcher, tmp_path, monkeypatch, reading, called):  # noqa: F811
    assert _keeper(switcher, tmp_path, monkeypatch, reading) == ([{"models": []}] if called else [])


def test_a_spend_control_block_with_weekly_room_calls_nothing(switcher, tmp_path, monkeypatch):  # noqa: F811
    reading = _codex(("work", True, 0.4), ("home", False, 0.4))
    for account in reading["accounts"]:
        account["usage"].update(blocked=True, usable=False, worst_used=1.0)  # blocked, but not weekly
    assert _keeper(switcher, tmp_path, monkeypatch, reading) == []


def test_the_weekly_reading_is_the_accounts_own_window():
    from lib import janitor_hotseat
    usage = {"windows": [{"label": "codex primary 5-hour", "used": 1.0},
                         {"label": "gpt-5-codex weekly", "used": 1.0},       # a model's window
                         {"label": "codex primary weekly", "used": 0.3}]}
    assert janitor_hotseat._codex_weekly_used(usage) == 0.3
    assert janitor_hotseat._codex_weekly_used({"windows": [{"label": "codex primary 5-hour", "used": 1}]}) is None
    assert janitor_hotseat.codex_weekly_spent([]) is False
    spent = janitor_hotseat.codex_weekly_spent
    assert spent([{"weekly_used": 0.99}, {"weekly_used": 1.0}]) is True    # 99 % used is spent
    assert spent([{"weekly_used": 0.98}, {"weekly_used": 1.0}]) is False


@pytest.mark.parametrize("unread", ["errored", "no weekly window"])
def test_an_account_the_host_cannot_read_authorises_nothing(switcher, tmp_path, monkeypatch, unread):  # noqa: F811
    """(F2) One account spent, the other's weekly reading missing: the Host does not know that no
    account has weekly room, so it asks for nothing."""
    reading = _codex(("work", True, 0.0), ("home", False, 0.0))
    usage = reading["accounts"][1]["usage"]
    if unread == "errored":
        usage.update(error="unreadable", usable=False, worst_used=None)
    else:
        usage["windows"] = [{"label": "codex primary 5-hour", "used": 1.0}]
    assert _keeper(switcher, tmp_path, monkeypatch, reading) == []


# ---- the cap's value (F3) -------------------------------------------------------------------------

@pytest.mark.parametrize("line,cap,refused", [
    ("", 1, False),                                   # unset: one a day
    ("codex_reset_max_per_day = 0", 0, False),        # off
    ("codex_reset_max_per_day = 3", 3, False),
    ("codex_reset_max_per_day = true", 0, True),
    ("codex_reset_max_per_day = false", 0, True),
    ("codex_reset_max_per_day = 1.5", 0, True),
    ("codex_reset_max_per_day = 0.0", 0, True),
    ("codex_reset_max_per_day = -1", 0, True),
    ('codex_reset_max_per_day = "2"', 0, True),
])
def test_the_cap_is_parsed_and_a_value_that_is_not_a_whole_number_is_off(tmp_path, monkeypatch, line, cap, refused):
    """A redemption cannot be undone: a cap the Host cannot read is 0 (off) with a log line, never 1."""
    logged = []
    from lib import log
    monkeypatch.setattr(log, "log", lambda tag, message="", *a, **k: logged.append((tag, message)))
    path = tmp_path / "config.toml"
    path.write_text('[agents]\ncodex_reset_command = ["broker"]\n' + line + "\n")
    loaded = config.load(path)
    assert loaded.codex_reset_max_per_day == cap and loaded.codex_reset_command == ("broker",)
    assert bool([t for t, _ in logged if t == "config.codex_reset_max_per_day"]) is refused


# ---- a reset count that cannot be read (F4) -------------------------------------------------------

@pytest.mark.parametrize("ledger", ["garbage", "{}", "5", '"x"', "[null]"])
def test_a_reset_count_that_cannot_be_read_redeems_nothing_and_is_told_once(
        switcher, tmp_path, monkeypatch, ledger):  # noqa: F811
    owner, worker, readings, calls, sent = switcher
    receipt, requests = _broker(tmp_path, monkeypatch, cap=5)
    receipt.write_text(json.dumps({"status": "redeemed", "verified": True, "account": "work"}))
    worker.reset_worker = janitor_reset.ResetWorker(worker.stop_event)
    settings_store.set_text("hotseat-reset.codex.redeemed", ledger)
    logged = []
    from lib import log
    monkeypatch.setattr(log, "log", lambda tag, message="", *a, **k: logged.append(tag))
    clock = [db.now_ms() + 1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    readings["codex"] = _codex(("work", True, 0.0), ("home", False, 0.0))
    for _ in range(3):
        _keeper_run(worker, clock)
        clock[0] += 20 * 60_000
    assert _requests(requests) == []                                      # fails closed: no call
    wanted = "Codex: the reset count cannot be read; no automatic reset until it is repaired."
    assert [n["preview"] for n in sent].count(wanted) == 1
    assert logged.count("bankedResetLedgerUnreadable") == 1
    assert settings_store.get_text("hotseat-reset.codex.redeemed") == ledger   # left for repair

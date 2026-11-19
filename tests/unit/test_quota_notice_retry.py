"""A quota notice that reached no phone because the transport failed is tried again (R105).

deliver_notifications stored whatever notify returned as the receipt's delivery, and that settled
it. apns.send_user_notification counts a relay that is down as failed and returns
{enabled: True, sent: 0, failed: N}, so the owner whose relay blinked at that moment was never told
that a window ran low. Now such a notice stays undelivered and is tried again at the keeper's
later looks, no more often than its interval, until a device takes it, the window it speaks of has
reset (expired), a newer crossing of that window replaces it (superseded), or the janitor's
generation changes (cancelled). A notice some device took is delivered; one nobody was meant to
get is settled as before."""
from __future__ import annotations

import json

import pytest

from lib import db, janitor_builtins, janitors, settings_store
from lib import janitor_autonomy as service

T0 = 1_795_082_400_000                    # 2026-11-19T10:00:00Z
DOWN = {"enabled": True, "sent": 0, "failed": 2, "disabled": 0}
UP = {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    return now


@pytest.fixture
def owner(monkeypatch, clock):
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    owners = janitor_builtins.ensure_builtins(initial={"quota-monitor": {"enabled": True}})
    service.setup()
    return owners["quota-monitor"]


def _interval(owner):
    return owner["options"]["interval_seconds"] * 1000


class Transport:
    """notify(): answers in turn from `answers`, the last one for ever after."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return dict(answer)


def _usage(used=80, observed=T0, resets=None, window_id="five-hour"):
    window = {"window_id": window_id, "used_percentage": used, "observed_at": _iso(observed),
              "freshness": "fresh"}
    if resets is not None:
        window["resets_at"] = _iso(resets)
    return {"providers": {"codex": {"provider_instance_id": "opaque-account", "windows": [window]}}}


def _look(worker, clock, at):
    clock[0] = at
    settings_store.set_int("quota-keeper.last-check", 0)
    key = "janitor-adaptive.quota-keeper"
    state = json.loads(settings_store.get_text(key, default="") or "{}")
    state["next_run_at"] = 0
    settings_store.set_text(key, json.dumps(state))
    worker.quota_once()


def _receipts():
    return [{"id": r["receipt_id"], "payload": json.loads(r["payload_json"]),
             "delivery": None if r["delivery_json"] is None else json.loads(r["delivery_json"])}
            for r in db.conn().execute("SELECT * FROM janitor_quota_receipts ORDER BY created_at, receipt_id")]


def _worker(transport, usage):
    return service.AutonomyJanitors(lambda *_: True, transport, usage_read=lambda: usage[0])


def test_the_relay_down_at_the_first_look_and_up_at_the_second_pushes_once_at_the_second(owner, clock):
    transport = Transport(DOWN, UP)
    usage = [_usage()]
    worker = _worker(transport, usage)
    _look(worker, clock, T0)
    [receipt] = _receipts()
    assert receipt["delivery"] is None                                    # still undelivered
    assert receipt["payload"]["delivery_retry"]["attempts"] == 1
    assert receipt["payload"]["delivery_retry"]["last"] == {"sent": 0, "failed": 2, "disabled": 0}
    _look(worker, clock, T0 + _interval(owner))
    assert len(transport.calls) == 2                                      # the push, at the second look
    [receipt] = _receipts()
    assert receipt["delivery"]["sent"] == 1 and receipt["delivery"]["attempts"] == 2
    assert "delivery_retry" not in transport.calls[1]                     # the phone is sent the notice only
    _look(worker, clock, T0 + 3 * _interval(owner))
    assert len(transport.calls) == 2                                      # delivered: never again


def test_a_retry_is_no_more_often_than_the_keepers_interval(owner, clock):
    transport = Transport(DOWN)
    usage = [_usage()]
    worker = _worker(transport, usage)
    _look(worker, clock, T0)
    clock[0] = T0 + 1_000
    worker.deliver_notifications(owner)
    worker.deliver_notifications(owner)
    assert len(transport.calls) == 1
    clock[0] = T0 + _interval(owner)
    worker.deliver_notifications(owner)
    assert len(transport.calls) == 2 and _receipts()[0]["payload"]["delivery_retry"]["attempts"] == 2


def test_a_notice_down_until_its_window_resets_is_settled_expired_and_never_pushed_after(owner, clock):
    transport = Transport(DOWN)
    usage = [_usage(resets=T0 + 2 * _interval(owner) - 1)]
    worker = _worker(transport, usage)
    _look(worker, clock, T0)
    _look(worker, clock, T0 + _interval(owner))
    assert len(transport.calls) == 2
    transport.answers = [UP]
    _look(worker, clock, T0 + 2 * _interval(owner))                      # the window has reset
    assert len(transport.calls) == 2                                      # no push about a window that is over
    [receipt] = _receipts()
    assert receipt["delivery"]["expired"] and receipt["delivery"]["attempts"] == 2


def test_a_newer_crossing_of_the_same_window_replaces_the_one_still_waiting(owner, clock):
    transport = Transport(DOWN, DOWN, UP)
    usage = [_usage(used=80)]
    worker = _worker(transport, usage)
    _look(worker, clock, T0)
    usage[0] = _usage(used=100, observed=T0 + _interval(owner))          # exhausted: a newer crossing
    _look(worker, clock, T0 + _interval(owner))
    older, newer = _receipts()
    assert newer["delivery"]["sent"] == 1 and "0% quota remaining" in transport.calls[-1]["preview"]
    assert older["delivery"]["superseded"] == newer["id"]
    assert [c["notification_id"] for c in transport.calls].count(newer["id"]) == 1
    _look(worker, clock, T0 + 3 * _interval(owner))
    assert len(transport.calls) == 3                                      # nothing tried again


def test_a_partial_failure_is_delivered_and_not_tried_again(owner, clock):
    transport = Transport({"enabled": True, "sent": 1, "failed": 1, "disabled": 0})
    worker = _worker(transport, [_usage()])
    _look(worker, clock, T0)
    _look(worker, clock, T0 + 3 * _interval(owner))
    assert len(transport.calls) == 1
    assert _receipts()[0]["delivery"] == {"enabled": True, "sent": 1, "failed": 1, "disabled": 0}


@pytest.mark.parametrize("answer", [
    {"enabled": False, "sent": 0, "failed": 0, "disabled": 0},
    {"enabled": True, "sent": 0, "failed": 0, "disabled": 0},
    {"enabled": True, "sent": 0, "failed": 0, "disabled": 2},
    {"enabled": True, "sent": 0, "failed": 0, "disabled": 0, "suppressed": True, "reason": "desktop-active"},
    {"enabled": True, "sent": 0, "failed": 1, "disabled": 0, "suppressed": True, "reason": "desktop-active"},
], ids=["push off", "no device", "every device gone", "desktop active", "desktop active mid-batch"])
def test_a_notice_nobody_was_meant_to_get_is_settled_as_before(owner, clock, answer):
    transport = Transport(answer)
    worker = _worker(transport, [_usage()])
    _look(worker, clock, T0)
    _look(worker, clock, T0 + 3 * _interval(owner))
    assert len(transport.calls) == 1 and _receipts()[0]["delivery"] == answer


def test_a_waiting_notice_is_cancelled_when_the_janitors_generation_changes(owner, clock):
    transport = Transport(DOWN)
    worker = _worker(transport, [_usage()])
    _look(worker, clock, T0)
    changed = janitors.configure(owner["session"], owner["revision"], options={"remaining_threshold": 30})
    clock[0] = T0 + _interval(owner)
    worker.deliver_notifications({**owner, "generation": changed["generation"]})
    assert len(transport.calls) == 1
    assert _receipts()[0]["delivery"] == {"cancelled": "Janitor configuration changed"}


def test_an_unreadable_receipt_is_set_aside_and_the_others_are_delivered(owner, clock):
    good = {"agent_id": owner["agent_id"], "owner_generation": owner["generation"], "notification_id": "good",
            "reason": "quota_threshold"}
    for receipt_id, payload in (("broken", "{not json"), ("a list", "[1]"), ("good", json.dumps(good))):
        db.conn().execute("INSERT INTO janitor_quota_receipts VALUES (?,?,?,NULL,?)", (receipt_id, "run", payload, T0))
    transport = Transport(UP)
    worker = _worker(transport, [_usage()])
    worker.deliver_notifications(owner)
    assert [p["notification_id"] for p in transport.calls] == ["good"]
    rows = dict(db.conn().execute("SELECT receipt_id, delivery_json FROM janitor_quota_receipts").fetchall())
    assert json.loads(rows["broken"]) == json.loads(rows["a list"]) == {"cancelled": "The receipt could not be read"}
    worker.deliver_notifications(owner)
    assert len(transport.calls) == 1

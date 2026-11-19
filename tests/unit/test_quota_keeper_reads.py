"""The quota keeper's look (R100). A window whose observed_at is not text is passed over and logged,
and the others are read; whatever raises inside the look, the run it opened is closed with its
reason, what was read before is delivered, and the next look works. A text moment is read as before
(one reader for moments is R104).

A window's observed_at was read by datetime.fromisoformat on observed_at.replace('Z', ...) inside
a try that caught only KeyError and ValueError: a window dated with nothing (None, a number, a
list) raised AttributeError out of the look, left its run running, and broke every later look
while the reading held that window."""
from __future__ import annotations

import json

import pytest

from lib import db, janitor_builtins, janitors, settings_store
from lib import janitor_autonomy as service

GOOD = "2026-11-19T10:00:00Z"            # the Host's own form (backend_usage_store._normalize_time)
GOOD_TOO = "2026-11-19T10:00:00.123456+00:00"


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    owners = janitor_builtins.ensure_builtins(initial={"quota-monitor": {"enabled": True}})
    service.setup()
    return owners["quota-monitor"]


@pytest.fixture
def logged(monkeypatch):
    from lib import log as log_module
    lines = []
    real = log_module.log

    def log(event, detail="", *args, **kwargs):
        lines.append((event, detail))
        return real(event, detail, *args, **kwargs)
    monkeypatch.setattr(log_module, "log", log)
    return lines


def _window(window_id, observed_at, used=80):
    return {"window_id": window_id, "used_percentage": used, "observed_at": observed_at, "freshness": "fresh"}


def _usage(*windows):
    return {"providers": {"codex": {"provider_instance_id": "opaque-account", "windows": list(windows)}}}


def _worker(usage, sent):
    return service.AutonomyJanitors(lambda *_: True, lambda payload: sent.append(payload) or {"sent": 1},
                                    usage_read=lambda: usage)


def _runs(owner):
    return janitors.list_runs(owner["session"])


def _look_again():
    """The keeper's lane due again now (its adaptive cadence kept)."""
    settings_store.set_int("quota-keeper.last-check", 0)
    key = "janitor-adaptive.quota-keeper"
    state = json.loads(settings_store.get_text(key, default="") or "{}")
    state["next_run_at"] = 0
    settings_store.set_text(key, json.dumps(state))


def _passed_over(logged):
    return [detail for event, detail in logged if event == "quotaWindowPassedOver"]


BAD = [None, 0, 12.5, ["2026-11-19T10:00:00Z"], {"at": GOOD}, True]
BAD_IDS = ["None", "zero", "a fraction", "a list", "a mapping", "a boolean"]


@pytest.mark.parametrize("bad", BAD, ids=BAD_IDS)
def test_a_window_dated_with_no_text_is_passed_over_and_the_others_are_read(owner, logged, bad):
    sent = []
    _worker(_usage(_window("first", GOOD), _window("odd", bad), _window("last", GOOD_TOO)), sent).quota_once()
    assert sorted(p["notification_id"] for p in sent) == sorted(
        r[0] for r in db.conn().execute("SELECT receipt_id FROM janitor_quota_receipts"))
    assert len(sent) == 2                                          # the two good windows, read and delivered
    [run] = _runs(owner)
    assert run["status"] == "completed"                            # the run is closed
    [line] = _passed_over(logged)                                  # once, with what and why; no content
    assert "provider=codex" in line and "window=odd" in line and "reason=" in line
    assert "2026" not in line


def test_the_same_odd_window_is_logged_once_per_look(owner, logged):
    sent = []
    worker = _worker(_usage(_window("odd", None), _window("good", GOOD)), sent)
    worker.quota_once()
    _look_again()
    worker.quota_once()
    assert len(_passed_over(logged)) == 2                          # one per look, not one per read


def test_whatever_raises_inside_the_look_closes_the_run_and_delivers_what_was_read(owner, monkeypatch, logged):
    sent = []
    real = service.quota_crossing
    seen = []

    def crossing(provider, account, window, *args):
        seen.append(window["window_id"])
        if window["window_id"] == "second" and len(seen) <= 3:
            raise RuntimeError("planted")
        return real(provider, account, window, *args)
    monkeypatch.setattr(service, "quota_crossing", crossing)
    usage = _usage(_window("first", GOOD), _window("second", GOOD), _window("third", GOOD))
    worker = _worker(usage, sent)
    with pytest.raises(RuntimeError, match="planted"):            # the loop logs it, as before
        worker.quota_once()
    [run] = _runs(owner)
    assert run["status"] == "failed" and "RuntimeError" in run["error"]   # closed, with its reason
    receipts = [r[0] for r in db.conn().execute("SELECT receipt_id FROM janitor_quota_receipts")]
    assert len(receipts) == 1 and [p["notification_id"] for p in sent] == receipts   # kept and delivered, once
    _look_again()
    worker.quota_once()                                            # the next look works
    newest = _runs(owner)[0]
    assert newest["status"] == "completed" and len(sent) == 3


def test_a_window_with_no_moment_at_all_is_passed_over_and_logged(owner, logged):
    sent = []
    usage = _usage(_window("first", GOOD), {"window_id": "bare", "used_percentage": 80, "freshness": "fresh"},
                   _window("last", GOOD_TOO))
    _worker(usage, sent).quota_once()
    assert len(sent) == 2 and _runs(owner)[0]["status"] == "completed"
    assert ["window=bare" in line for line in _passed_over(logged)] == [True]


def test_a_push_that_raises_at_the_looks_delivery_is_tried_once_in_that_look_and_once_at_the_next(owner):
    """The close delivers only what was read before a raise: a raise from the look's own delivery
    is not delivered again at once (a second push could reach a phone that took the first)."""
    calls = []

    def notify(payload):
        calls.append(payload["notification_id"])
        if len(calls) == 1:
            raise RuntimeError("the transport raised")
        return {"sent": 1}
    worker = service.AutonomyJanitors(lambda *_: True, notify, usage_read=lambda: _usage(_window("first", GOOD)))
    with pytest.raises(RuntimeError, match="the transport raised"):
        worker.quota_once()
    assert len(calls) == 1                                          # one attempt in that look
    [run] = _runs(owner)
    assert run["status"] == "failed"
    assert db.conn().execute("SELECT delivery_json FROM janitor_quota_receipts").fetchone()[0] is None
    _look_again()
    worker.quota_once()
    assert len(calls) == 2 and calls[0] == calls[1]                 # the same notice, once more, at the next look
    assert db.conn().execute("SELECT delivery_json FROM janitor_quota_receipts").fetchone()[0] == '{"sent": 1}'

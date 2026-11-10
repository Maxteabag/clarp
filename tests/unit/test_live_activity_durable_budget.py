"""APNS-N3 Step2 storage/restart owner: real disposable SQLite, no real push."""
from __future__ import annotations

import sqlite3

from lib import apns, db, settings_store
from tests.unit.test_live_activity_budget import TOKENS, _register, _wire_refusal, world  # noqa: F401


def _fresh_gate(monkeypatch, clock):
    monkeypatch.setattr(apns, "_LA_GATE", apns._LiveActivityGate(
        clock=clock, schedule=clock.schedule))


def _trusted_day(monkeypatch, clock):
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + clock.now - start)


def test_local_restarts_cannot_spend_more_than_300_shared_daily_reservations(world, monkeypatch):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    for restart in range(31):
        _fresh_gate(monkeypatch, clock)
        for item in range(10):
            apns.send_live_activity({"n": restart * 10 + item})
    assert len(sender.sent) == 300


def test_day_pause_survives_a_fresh_gate_using_the_same_database(world, monkeypatch):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    assert _wire_refusal(monkeypatch, header="43200")[0] == 429
    _fresh_gate(monkeypatch, clock)
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    assert not sender.sent


def test_settings_read_failure_sends_no_la_and_preserves_busy_timeout(world, monkeypatch):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    c = db.conn()
    previous = c.execute("PRAGMA busy_timeout").fetchone()[0]
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic read failure")
    monkeypatch.setattr(settings_store, "get", unavailable)
    apns.send_live_activity({"n": 1})
    assert not sender.sent
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == previous


def test_la_does_not_commit_or_rollback_a_borrowed_caller_transaction(world, monkeypatch):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO settings(key,value,updated_at) VALUES ('synthetic.caller','held',1)")
    try:
        apns.send_live_activity({"n": 1})
        assert not sender.sent
        assert c.in_transaction
        assert settings_store.get("synthetic.caller") == "held"
    finally:
        c.execute("ROLLBACK")
    assert settings_store.get("synthetic.caller") is None


KEY = "apns.live_activity_day_budget.v1"
NOW = 1793923200.0


def _state(**changes):
    value = {"version": 1, "day": int(NOW // 86400), "count": 0,
             "paused_until": 0.0, "last_wall": NOW}
    value.update(changes)
    return value


def _put(value):
    import json
    settings_store.set_text(KEY, json.dumps(value))


def test_only_one_content_free_internal_key_is_written_and_commit_precedes_network(world, monkeypatch):
    import json
    clock, _ = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    observed = []
    class Sender:
        def send(self, *args, **kwargs):
            assert not db.conn().in_transaction
            value = settings_store.get(KEY)
            observed.append(json.loads(value) if value else None)
            return 200, "", ""
        def reset(self):
            pass
    monkeypatch.setattr(apns, "_transport", lambda cfg: Sender())
    apns.send_live_activity({"private": "DO_NOT_STORE"})
    assert observed == [_state(count=1)]
    stored = settings_store.get(KEY)
    assert "DO_NOT_STORE" not in stored and all(token not in stored for token in TOKENS)


import pytest


@pytest.mark.parametrize("raw", [
    "not-json", "null", "[]", "{}",
    '{"version":2}', '{"version":true}',
])
def test_unusable_or_unknown_protocol_does_not_create_fresh_credit(world, monkeypatch, raw):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    settings_store.set_text(KEY, raw)
    apns.send_live_activity({"n": 1})
    assert not sender.sent
    assert settings_store.get(KEY) == raw


@pytest.mark.parametrize("changes", [
    {"version": True}, {"count": True}, {"count": -1}, {"count": 301},
    {"day": True}, {"day": int(NOW // 86400) + 1},
    {"last_wall": NOW + 1}, {"last_wall": float("nan")},
    {"last_wall": NOW - 86400}, {"paused_until": -1},
    {"paused_until": float("inf")}, {"paused_until": True},
    {"unexpected": "DO_NOT_REWRITE"},
])
def test_invalid_or_future_state_remains_fail_closed(world, monkeypatch, changes):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    _put(_state(**changes))
    before = settings_store.get(KEY)
    apns.send_live_activity({"n": 1})
    assert not sender.sent
    assert settings_store.get(KEY) == before


def test_trusted_forward_midnight_resets_count_once_and_carries_later_pause(world, monkeypatch):
    import json
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    _put(_state(count=300, paused_until=NOW + 86400 + 100))
    clock.advance(86400)
    apns.send_live_activity({"n": 1})
    assert not sender.sent
    _fresh_gate(monkeypatch, clock)
    clock.advance(100)
    apns.send_live_activity({"n": 2})
    assert len(sender.sent) == 1
    value = json.loads(settings_store.get(KEY))
    assert value["count"] == 1 and value["day"] == int(NOW // 86400) + 1
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 3})
    assert json.loads(settings_store.get(KEY))["count"] == 2


@pytest.mark.parametrize("after_commit", [False, True])
def test_uncertain_or_failed_commit_never_authorizes_network(world, monkeypatch, after_commit):
    import json
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    connection = db.conn()
    original = settings_store.conn
    class Wrapped:
        def execute(self, sql, *args, **kwargs):
            if sql == "COMMIT":
                if after_commit:
                    connection.execute(sql)
                raise sqlite3.OperationalError("synthetic commit uncertainty")
            return connection.execute(sql, *args, **kwargs)
    monkeypatch.setattr(settings_store, "conn", lambda: Wrapped())
    apns.send_live_activity({"n": 1})
    assert not sender.sent
    assert not connection.in_transaction
    monkeypatch.setattr(settings_store, "conn", original)
    raw = settings_store.get(KEY)
    assert (json.loads(raw)["count"] if raw else 0) == (1 if after_commit else 0)


def test_unused_batch_suffix_is_not_reserved_after_first_day_refusal(world, monkeypatch):
    import json
    from tests.unit.test_live_activity_budget import THIRD
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(*TOKENS, THIRD)
    sender.status, sender.reason = 429, "push_rate_limited"
    sender.on_send = lambda: _wire_refusal(monkeypatch, header="43200")
    apns.send_live_activity({"n": 1})
    assert len(sender.sent) == 1
    assert json.loads(settings_store.get(KEY))["count"] == 1


def _competing_process(path, barrier, result):
    """Independent Python/SQLite owner, synthetic transport; no service/provider."""
    import pathlib
    from types import SimpleNamespace
    from lib import config, live_activity
    from tests.unit.test_live_activity_push import FakeClock
    count = 0
    try:
        db.reset_for_tests(pathlib.Path(path))
        db.conn()
        config.load = lambda: SimpleNamespace(apns_enabled=lambda: True, apns_environment="production")
        apns.time.time = lambda: NOW
        apns.log = lambda *args, **kwargs: None
        class Sender:
            def send(self, *args, **kwargs):
                nonlocal count
                assert not db.conn().in_transaction
                count += 1
                return 200, "", ""
            def reset(self):
                pass
        apns._transport = lambda cfg: Sender()
        clock = FakeClock()
        barrier.wait(10)
        for n in range(400):
            apns._LA_GATE = apns._LiveActivityGate(clock=clock, schedule=clock.schedule)
            apns.send_live_activity({"n": n})
        result.put({"sent": count})
    except BaseException as exc:
        result.put({"error": type(exc).__name__, "sent": count})
    finally:
        db.close_local()


def test_two_processes_share_exactly_300_committed_reservations(world, monkeypatch):
    import multiprocessing
    from pathlib import Path
    clock, _ = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    path = str(db.DB_PATH)
    assert Path(path).name == "state.sqlite"
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_competing_process, args=(path, barrier, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        values = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(5)
        assert all(not worker.is_alive() and worker.exitcode == 0 for worker in workers)
        assert all("error" not in value for value in values), values
        assert sum(value["sent"] for value in values) == 300
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("push_type", ["liveactivity", "alert"])
@pytest.mark.parametrize("route", ["auto", "grant"])
def test_uncertain_la_transport_does_not_fallback_with_the_same_reservation(world, monkeypatch, push_type, route):
    from types import SimpleNamespace
    calls = []
    class Relay:
        name = "relay"
        def send(self, *args, **kwargs):
            calls.append("relay")
            raise apns._RelayUnavailable("synthetic unknown outcome")
        def reset(self):
            pass
    class Direct:
        name = "direct"
        def send(self, *args, **kwargs):
            calls.append("direct")
            return 200, "", ""
    monkeypatch.setattr(apns, "_RelayTransport", lambda cfg: Relay())
    monkeypatch.setattr(apns, "_GrantTransport", lambda cfg: Relay())
    monkeypatch.setattr(apns, "_DirectTransport", lambda cfg: Direct())
    monkeypatch.setattr(apns, "_base_transport", lambda cfg: None)
    cfg = SimpleNamespace(apns_direct_ready=lambda: True, apns_mode="auto", apns_relay_url_ok=lambda: True)
    transport = apns._AutoTransport(cfg) if route == "auto" else apns._Router(cfg)
    kwargs = {} if route == "auto" else {"grant": "pg1." + "1" * 64 + "." + "2" * 16 + "." + "3" * 64}
    if push_type == "liveactivity":
        with pytest.raises(apns._RelayUnavailable):
            transport.send(TOKENS[0], "production", {"n": 1}, push_type=push_type, **kwargs)
        assert calls == ["relay"]
    else:
        assert transport.send(TOKENS[0], "production", {"n": 1}, push_type=push_type, **kwargs)[0] == 200
        assert calls == ["relay", "direct"]


def test_external_writer_does_not_gate_alert_result_and_failed_pause_can_be_retried(world, monkeypatch):
    import json
    import time
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    connection = db.conn()
    previous = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert _wire_refusal(monkeypatch, header="43200")[0] == 429
        # A generous latency tripwire: 20ms is a SQLite busy-wait cap, NOT
        # a promise that OS scheduling or a filesystem COMMIT takes 20ms.
        assert time.monotonic() - started < .5
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == previous
        apns.send_live_activity({"n": 1})
        assert not sender.sent
        assert settings_store.get(KEY) is None
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert _wire_refusal(monkeypatch, header="43200")[0] == 429  # event-driven retry, no new thread
    assert json.loads(settings_store.get(KEY))["paused_until"] == NOW + 86400
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 2})
    assert not sender.sent


def test_failed_pause_write_does_not_falsely_claim_a_restart_learned_the_pause(world, monkeypatch):
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    db.conn()
    writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert _wire_refusal(monkeypatch, header="43200")[0] == 429
        apns.send_live_activity({"n": 1})
        assert not sender.sent
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    # This declared limit is intentional: no committed SQLite write could
    # have informed a fresh process. Its own atomic 300 cap still applies.
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 2})
    assert len(sender.sent) == 1


def test_exhausted_day_has_one_wakeup_and_sends_only_latest_state_after_rollover(world, monkeypatch):
    import json
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    _put(_state(count=300))
    for n in range(100):
        apns.send_live_activity({"n": n})
    assert not sender.sent
    assert len([timer for timer in clock.timers if not timer[2]]) == 1
    clock.advance(86399)
    assert not sender.sent
    clock.advance(1)
    assert len(sender.sent) == 1 and sender.sent[0][1] == {"n": 99}
    assert json.loads(settings_store.get(KEY))["count"] == 1


def test_wall_rollback_then_forward_skip_never_creates_backdated_credit(world, monkeypatch):
    import json
    clock, sender = world
    wall = [NOW + 100]
    monkeypatch.setattr(apns.time, "time", lambda: wall[0])
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    wall[0] = NOW + 99
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 2})
    assert len(sender.sent) == 1
    assert json.loads(settings_store.get(KEY))["last_wall"] == NOW + 100
    wall[0] = NOW + 3 * 86400
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 3})
    value = json.loads(settings_store.get(KEY))
    assert value["count"] == 1 and value["day"] == int(wall[0] // 86400)


def test_crash_after_committed_reservation_never_reuses_its_credit(world, monkeypatch):
    import json
    clock, sender = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    original = sender.send
    def crash(*args, **kwargs):
        assert json.loads(settings_store.get(KEY))["count"] == 1
        raise SystemExit("synthetic crash before transport")
    monkeypatch.setattr(sender, "send", crash)
    with pytest.raises(SystemExit):
        apns.send_live_activity({"n": 1})
    assert not sender.sent
    monkeypatch.setattr(sender, "send", original)
    _fresh_gate(monkeypatch, clock)
    apns.send_live_activity({"n": 2})
    assert len(sender.sent) == 1
    assert json.loads(settings_store.get(KEY))["count"] == 2


def test_utc_day_snapshot_is_taken_after_the_reservation_acquires_the_writer(world, monkeypatch):
    import json
    clock, sender = world
    wall = [NOW + 86399.99]
    monkeypatch.setattr(apns.time, "time", lambda: wall[0])
    _register(TOKENS[0])
    _put(_state(count=300, last_wall=wall[0]))
    connection = db.conn()
    class Wrapped:
        def execute(self, sql, *args, **kwargs):
            result = connection.execute(sql, *args, **kwargs)
            if sql == "BEGIN IMMEDIATE":
                wall[0] = NOW + 86400.01  # day boundary during writer acquisition
            return result
    monkeypatch.setattr(settings_store, "conn", lambda: Wrapped())
    apns.send_live_activity({"n": 1})
    assert len(sender.sent) == 1
    value = json.loads(settings_store.get(KEY))
    assert value["day"] == int(wall[0] // 86400) and value["count"] == 1


@pytest.mark.parametrize("remaining", [0, 1])
@pytest.mark.parametrize("failure", ["registration", "client-construction", "client-closed", "send-unconfigured"])
def test_known_unsent_auto_fallback_uses_one_available_permit(world, monkeypatch, remaining, failure):
    from types import SimpleNamespace
    clock, _ = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    _put(_state(count=300 - remaining))
    calls = []
    class RelayHTTP:
        def request(self, method, url, **kwargs):
            calls.append(("relay", url))
            if url.endswith("/v1/push/devices"):
                return SimpleNamespace(status_code=503) if failure == "registration" else SimpleNamespace(status_code=200)
            assert failure == "send-unconfigured" and url.endswith("/v1/push/send")
            return SimpleNamespace(status_code=404)
        def close(self):
            pass
    def client():
        if failure == "client-construction":
            raise RuntimeError("synthetic client not constructed")
        if failure == "client-closed":
            import httpx
            closed = httpx.Client()
            closed.close()
            return closed
        return RelayHTTP()
    class Direct:
        name = "direct"
        def __init__(self, cfg):
            pass
        def send(self, *args, **kwargs):
            assert not db.conn().in_transaction
            calls.append(("direct", kwargs.get("push_type")))
            return 200, "", "synthetic"
        def reset(self):
            pass
    apns._reset_relay_state()
    monkeypatch.setattr(apns, "_relay_client", client)
    monkeypatch.setattr(apns, "_DirectTransport", Direct)
    cfg = SimpleNamespace(audio_central_url="https://relay.invalid",
                          audio_central_credential="synthetic", apns_direct_ready=lambda: True)
    transport = apns._AutoTransport(cfg)
    monkeypatch.setattr(apns, "_transport", lambda cfg: transport)
    result = apns.send_live_activity({"n": 1})
    if remaining:
        assert result["sent"] == 1
        assert calls[-1] == ("direct", "liveactivity")
        assert sum(kind == "direct" for kind, _ in calls) == 1
        assert not any(url.endswith("/v1/push/send") for kind, url in calls if kind == "relay") or failure == "send-unconfigured"
    else:
        assert result["sent"] == 0 and calls == []
    import json
    assert json.loads(settings_store.get(KEY))["count"] == 300


@pytest.mark.parametrize("remaining_after_first", [0, 1])
def test_cached_relay_down_is_unsent_for_a_new_independently_reserved_la(world, monkeypatch, remaining_after_first):
    from types import SimpleNamespace
    clock, _ = world
    _trusted_day(monkeypatch, clock)
    _register(TOKENS[0])
    _put(_state(count=299 - remaining_after_first))
    calls = []
    class RelayHTTP:
        def request(self, method, url, **kwargs):
            calls.append(("relay", url))
            return SimpleNamespace(status_code=200 if url.endswith("/v1/push/devices") else 503)
        def close(self):
            pass
    class Direct:
        name = "direct"
        def __init__(self, cfg):
            pass
        def send(self, *args, **kwargs):
            calls.append(("direct", kwargs.get("push_type")))
            return 200, "", ""
        def reset(self):
            pass
    apns._reset_relay_state()
    monkeypatch.setattr(apns, "_relay_client", lambda: RelayHTTP())
    monkeypatch.setattr(apns, "_DirectTransport", Direct)
    transport = apns._AutoTransport(SimpleNamespace(audio_central_url="https://relay.invalid",
        audio_central_credential="synthetic", apns_direct_ready=lambda: True))
    monkeypatch.setattr(apns, "_transport", lambda cfg: transport)
    assert apns.send_live_activity({"n": 1})["failed"] == 1
    assert not any(kind == "direct" for kind, _ in calls)  # first push may have reached relay
    assert apns.send_live_activity({"n": 2})["sent"] == remaining_after_first
    assert sum(kind == "direct" for kind, _ in calls) == remaining_after_first
    assert sum(url.endswith("/v1/push/send") for kind, url in calls if kind == "relay") == 1


def test_cached_grant_down_allows_a_later_unsent_transport_fallback(world, monkeypatch):
    from types import SimpleNamespace
    calls = []
    class RelayHTTP:
        def request(self, *args, **kwargs):
            calls.append("grant")
            return SimpleNamespace(status_code=503)
    class Direct:
        name = "direct"
        def __init__(self, cfg):
            pass
        def send(self, *args, **kwargs):
            calls.append("direct")
            return 200, "", ""
    monkeypatch.setattr(apns, "_relay_client", lambda: RelayHTTP())
    monkeypatch.setattr(apns, "_base_transport", lambda cfg: None)
    monkeypatch.setattr(apns, "_DirectTransport", Direct)
    cfg = SimpleNamespace(audio_central_url="https://relay.invalid", apns_mode="auto",
                          apns_relay_url_ok=lambda: True, apns_direct_ready=lambda: True)
    router = apns._Router(cfg)
    grant = "pg1." + "1" * 64 + "." + "2" * 16 + "." + "3" * 64
    with pytest.raises(apns._RelayUnavailable):
        router.send(TOKENS[0], "production", {}, grant=grant, push_type="liveactivity")
    # Transport-only sibling control, not a newly invented public LA-grant route.
    assert router.send(TOKENS[0], "production", {}, grant=grant, push_type="liveactivity")[0] == 200
    assert calls == ["grant", "direct"]

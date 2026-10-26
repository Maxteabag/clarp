"""F4 fix-up (ROOT 538; coord review 1805024a notes a, c, d): the short busy
budget keeps an outer busy_expected on nesting, a failed timeout restore never
leaves a lowered connection in use, and the delivery hint follows the launch's
own bookkeeping. Isolated temp DB per test (conftest); no timing."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db
import lib.turn_dispatch as _td
from lib.turn_dispatch import DispatchCommand, TurnDispatchService


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()
    db._LOCAL.discard_conn = None
    db._LOCAL.busy_expected = False


# --- a: nesting keeps the outer busy_expected -------------------------------------------

def test_a_nested_short_budget_restores_the_outer_busy_expected():
    db._LOCAL.busy_expected = True                      # e.g. an outer best-effort write
    with db.busy_timeout_at_most(50):
        with db.busy_timeout_at_most(10):
            assert db._LOCAL.busy_expected is True
        assert db._LOCAL.busy_expected is True, "the inner exit must not clear the outer flag"
    assert db._LOCAL.busy_expected is True
    db._LOCAL.busy_expected = False
    with db.busy_timeout_at_most(50):
        pass
    assert db._LOCAL.busy_expected is False


# --- c: a failed restore never leaves a lowered connection in use ------------------------

class _Proxy:
    """A connection whose busy-timeout restore fails; everything else passes through."""

    def __init__(self, real, *, in_transaction=False):
        self.real, self.in_transaction, self.closed = real, in_transaction, False
        self.timeout = int(real.execute("PRAGMA busy_timeout").fetchone()[0])
        self.lowered_seen = None

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA busy_timeout = "):
            value = int(sql.rsplit("=", 1)[1])
            if value < self.timeout:
                self.lowered_seen = value
                self.current = value
                return None
            raise sqlite3.OperationalError("injected: restore failed")
        if sql == "PRAGMA busy_timeout":
            return SimpleNamespace(fetchone=lambda: (getattr(self, "current", self.timeout),))
        return self.real.execute(sql, *args)

    def close(self):
        self.closed = True


def _install(proxy):
    db._LOCAL.conn = proxy
    return proxy


def test_c_a_failed_restore_outside_a_transaction_reports_and_drops_the_connection(monkeypatch):
    real = db.conn()
    proxy = _install(_Proxy(real))
    reports = []
    import lib.log as log_module
    monkeypatch.setattr(log_module, "log_exception",
                        lambda name, exc, detail="": reports.append((name, detail)))
    with db.busy_timeout_at_most(50):
        assert proxy.lowered_seen == min(50, proxy.timeout)
    assert proxy.closed and getattr(db._LOCAL, "conn", None) is None, "never reused at 50 ms"
    (name, detail), = reports
    assert name == "busyTimeoutRestoreFail" and f"previous={proxy.timeout}" in detail
    assert "lowered=50" in detail and "observed=50" in detail and f"delta_ms={proxy.timeout - 50}" in detail
    fresh = db.conn()
    assert fresh is not proxy
    assert int(fresh.execute("PRAGMA busy_timeout").fetchone()[0]) == proxy.timeout
    real.close()


def test_c_a_failed_restore_inside_a_transaction_raises_and_replaces_after_it(monkeypatch):
    real = db.conn()
    proxy = _install(_Proxy(real, in_transaction=True))
    import lib.log as log_module
    monkeypatch.setattr(log_module, "log_exception", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="inside a transaction"):
        with db.busy_timeout_at_most(50):
            pass
    assert not proxy.closed, "closing would roll back the caller's transaction"
    assert db.conn() is proxy, "still the caller's connection while its transaction is open"
    proxy.in_transaction = False                       # the caller committed / rolled back
    fresh = db.conn()
    assert fresh is not proxy and proxy.closed
    assert int(fresh.execute("PRAGMA busy_timeout").fetchone()[0]) == proxy.timeout
    real.close()


# --- d: the hint follows the launch's own bookkeeping ------------------------------------

class _Backends:
    CLAUDE = "claude"

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return []

    def spawn_turn(self, backend, **kwargs):
        return SimpleNamespace(name="handle")


def test_d_the_spawn_hint_comes_after_the_handle_and_mark_injected(tmp_path, monkeypatch):
    from lib import team_store
    order = []
    monkeypatch.setattr(team_store, "mark_injected", lambda *a, **k: order.append("injected"))
    monkeypatch.setattr(_td, "_announce_delivery", lambda trace: order.append("hint"))
    agent = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    agents_db.start_runtime(agent, "mike")
    ctx = SimpleNamespace(default_session="mike", agents_path=tmp_path / "u.json",
                          stream=SimpleNamespace(broadcast=lambda e: None))
    service = TurnDispatchService(ctx, backend_registry=_Backends(), home=tmp_path,
                                  uuid_factory=lambda: "bs", retry_scheduler=lambda *_: None)
    service.submit(DispatchCommand(text="hi", requested_session="mike", trace_id="t1",
                                   client_msg_id="u-1", synthesize_audio=False, origin="user"))
    assert order == ["injected", "hint"]

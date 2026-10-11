"""GET /status ``runtime``: a stable, complete block, or an explicit
``unavailable`` one. Never an idle-looking block because the status RPC
failed (Solu re7d614160304, item 2; Nadia's 10:34:51 sample on 2026-10-11)."""
from __future__ import annotations

import socket

from lib.runtime_bridge import RuntimeProtocolError, RuntimeUnavailable
from lib.runtime_release import RuntimeStatusView

KEYS = {"available", "reason", "release_id", "fenced", "sealed", "draining",
        "drain", "active_turns", "observed_at"}


def _status(**over):
    status = {"release_id": "r1", "draining": False, "fenced": False,
              "drain": {"phase": "idle"}, "active": {"a": "t"}}
    status.update(over)
    return status


def _view(results, now=[1_700_000_000.0]):
    results = list(results)

    def fetch():
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
    return RuntimeStatusView(fetch, wall=lambda: now[0])


def test_a_fenced_runtime_reports_draining_before_it_seals():
    view = _view([_status(fenced=True, drain={"phase": "draining"})])
    block = view.block()
    assert set(block) == KEYS
    assert block["available"] is True
    assert block["fenced"] is True
    assert block["sealed"] is False
    assert block["draining"] is True
    assert block["reason"] == ""


def test_a_sealed_runtime_reports_sealed_and_draining():
    block = _view([_status(draining=True, fenced=True,
                           drain={"phase": "draining"})]).block()
    assert (block["fenced"], block["sealed"], block["draining"]) == (True, True, True)


def test_a_runtime_without_a_fence_field_is_read_from_its_drain_phase():
    block = _view([{"release_id": "r0", "draining": False,
                    "drain": {"phase": "draining"}, "active": {}}]).block()
    assert block["fenced"] is True and block["draining"] is True


def test_a_runtime_from_before_the_drain_says_so():
    block = _view([{"release_id": "r0", "draining": False, "active": {}}]).block()
    assert block["drain"] == {"phase": "unsupported"}
    assert block["draining"] is False


def test_a_timed_out_status_call_is_unavailable_never_idle():
    timeout = RuntimeUnavailable("agent runtime unavailable: timed out")
    timeout.__cause__ = socket.timeout("timed out")
    now = [1_700_000_000.0]
    view = _view([_status(fenced=True, drain={"phase": "draining"}), timeout], now)
    first = view.block()
    now[0] += 5
    block = view.block()
    assert set(block) == KEYS
    assert block["available"] is False
    assert block["reason"] == "timeout"
    # Unknown, not "not draining".
    assert block["fenced"] is None
    assert block["sealed"] is None
    assert block["draining"] is None
    assert block["active_turns"] is None
    assert block["drain"]["phase"] == "unavailable"
    # The last real answer, with its age, so a reader can judge it.
    assert block["drain"]["last_known"] == first["drain"]
    assert block["drain"]["last_known_at"] == first["observed_at"]
    assert block["release_id"] == "r1"


def test_unavailable_reasons_are_told_apart():
    blocks = [_view([error]).block() for error in (
        RuntimeUnavailable("agent runtime unavailable: no socket"),
        RuntimeProtocolError("invalid runtime response"),
        ValueError("boom"))]
    assert [b["reason"] for b in blocks] == ["unreachable", "protocol_error", "error"]
    assert all(b["drain"] == {"phase": "unavailable", "last_known": None,
                              "last_known_at": None} for b in blocks)


def test_an_empty_answer_is_unavailable_not_an_empty_block():
    block = _view([{}]).block()
    assert block["available"] is False
    assert block["reason"] == "empty_status"

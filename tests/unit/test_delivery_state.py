"""F4 delivery state (joint packet 9a96df74 + addendum 1): receipt rev/seq/pointer
transitions, the bounded delivery feed, bootstrap with a fixed ceiling, the prune
watermark and the Stop marker. Isolated DB per test (conftest), fake backend, no
timing: work is asserted by counting the SQL statements actually executed."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, launch_receipts as lr, settings_store, turn_queue
import lib.turn_dispatch as _td
from lib.turn_dispatch import TurnDispatchService


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


def _pending(agent="ag", trace="t1", cid="c1", gen=None):
    lr.record_pending(agent_id=agent, trace_id=trace, client_msg_id=cid, origin="user",
                      user_stop_generation=turn_queue.user_stop_generation(agent)
                      if gen is None else gen)
    return lr.get(trace)


def _pointers(agent="ag"):
    return settings_store.items_in_range(lr.POINTER_PREFIX + agent + ":",
                                         lr.POINTER_PREFIX + agent + ";", limit=10_000)


def _seq():
    return settings_store.get_int(lr.SEQ_KEY)


def _gen(agent="ag"):
    return turn_queue.user_stop_generation(agent)


def _normal(agent="ag", *, since, **kw):
    return lr.delivery_page(agent, since=since, stop_generation=_gen(agent), **kw)


class _Statements:
    """Counts the SQL statements this thread's connection executes."""

    def __enter__(self):
        self.sql: list[str] = []
        db.conn().set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *exc):
        db.conn().set_trace_callback(None)

    def reads(self, needle: str) -> int:
        return sum(1 for s in self.sql if s.lstrip().upper().startswith("SELECT") and needle in s)


# --- C1: every transition moves rev, seq and the pointer in ONE transaction ---------------

def test_c1_every_transition_moves_rev_seq_and_pointer_together():
    r = _pending()
    assert (r["state"], r["rev"], r["seq"]) == ("pending", 1, 1)
    assert _pointers() == [(lr.pointer_key("ag", 1), "t1")]
    assert lr.mark_attempted("t1") == "attempted"
    r = lr.get("t1")
    assert (r["state"], r["rev"], r["seq"]) == ("attempted", 2, 2)
    assert _pointers() == [(lr.pointer_key("ag", 2), "t1")], "the old pointer moved, not copied"
    assert lr.mark_attempted("t1") == "already_attempted"
    assert (lr.get("t1")["rev"], _seq()) == (2, 2), "a no-op is not a transition"


def test_c1_a_failure_after_the_increment_rolls_back_seq_rev_and_pointer(monkeypatch):
    _pending()
    real = settings_store.set_text

    def failing(key, value, **kw):
        if key.startswith(lr.POINTER_PREFIX):
            raise RuntimeError("injected pointer write failure")
        return real(key, value, **kw)
    monkeypatch.setattr(settings_store, "set_text", failing)
    with pytest.raises(RuntimeError):
        lr.mark_attempted("t1")
    monkeypatch.setattr(settings_store, "set_text", real)
    r = lr.get("t1")
    assert (r["state"], r["rev"], r["seq"], _seq()) == ("pending", 1, 1, 1)
    assert _pointers() == [(lr.pointer_key("ag", 1), "t1")]
    assert not db.conn().in_transaction


def test_global_counter_contended_writers_get_unique_gapless_seqs(tmp_path):
    errors: list[BaseException] = []

    def writer(n):
        try:
            with db.busy_timeout(30_000):
                for i in range(20):
                    lr.record_pending(agent_id=f"a{n}", trace_id=f"t{n}-{i}",
                                      client_msg_id=f"c{n}-{i}", origin="user")
                    lr.mark_attempted(f"t{n}-{i}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close_local()
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    seqs = sorted(lr.get(f"t{n}-{i}")["seq"] for n in range(4) for i in range(20))
    assert seqs == sorted(set(seqs)) and _seq() == 160, "160 transitions, 160 seqs"
    pointer_seqs = sorted(int(k[-20:]) for n in range(4) for k, _ in _pointers(f"a{n}"))
    assert pointer_seqs == seqs, "exactly one pointer per receipt, at its latest seq"


# --- C2 legacy, projection --------------------------------------------------------------

def _legacy(trace="tl", agent="ag", state="pending"):
    settings_store.set_text(lr.PREFIX + trace, json.dumps({
        "state": state, "agent_id": agent, "trace_id": trace, "client_msg_id": "cl",
        "origin": "user", "sender_agent_id": "", "admitted_at": 1, "user_stop_gen": 0}))


def test_c2_legacy_receipt_is_omitted_until_its_first_real_transition():
    _legacy()
    assert lr.project(lr.get("tl"), agent_id="ag", user_stop_generation=0) is None
    assert lr.status("ag", "tl") == "retryable", "retry authority is unchanged"
    assert lr.mark_attempted("tl") == "attempted"
    r = lr.get("tl")
    assert (r["rev"], r["seq"]) == (1, 1), "no invented history"
    assert lr.project(r, agent_id="ag", user_stop_generation=0)["state"] == "handed_over"


def test_projection_states_and_mismatches_are_unknown():
    r = _pending()
    shown = lr.project(r, agent_id="ag", user_stop_generation=0)
    assert shown == {"state": "pending_launch", "rev": 1, "seq": 1,
                     "for": {"client_msg_id": "c1", "trace_id": "t1"}}
    assert lr.project(r, agent_id="ag", user_stop_generation=1)["state"] == "stopped"
    assert lr.project(r, agent_id="other", user_stop_generation=0) is None
    assert lr.project(r, agent_id="ag", user_stop_generation=0, origin="agent") is None
    assert lr.project(r, agent_id="ag", user_stop_generation=0, sender_agent_id="x") is None
    assert lr.project(dict(r, state="relaunched"), agent_id="ag", user_stop_generation=0) is None
    assert lr.project(dict(r, rev=True), agent_id="ag", user_stop_generation=0) is None


# --- C6 Stop, fence batch bounds -----------------------------------------------------------

def test_c6_user_stop_fences_with_a_new_seq_and_the_override_hands_over():
    _pending()
    turn_queue.set_paused("ag", True, user_stop=True)      # generation 1 + stop marker
    assert lr.fence_agent("ag") == 1
    r = lr.get("t1")
    assert (r["state"], r["rev"]) == ("fenced", 2)
    assert lr.mark_attempted("t1", override_fence=True) == "attempted"
    r = lr.get("t1")
    assert (r["state"], r["rev"]) == ("attempted", 3)
    assert lr.project(r, agent_id="ag", user_stop_generation=1)["state"] == "handed_over"


def test_c6_a_maintenance_cancel_moves_no_generation_and_writes_no_marker():
    _pending()
    before = _seq()
    turn_queue.set_paused("ag", True)                       # not the user's Stop
    assert turn_queue.user_stop_generation("ag") == 0 and _seq() == before


def test_fence_batch_reads_the_prefix_once_and_writes_per_pending_receipt():
    for i in range(3):
        _pending(trace=f"t{i}", cid=f"c{i}")
    for i in range(4):
        _pending(agent="other", trace=f"o{i}", cid=f"o{i}")
    lr.mark_attempted("t2")
    with _Statements() as st:
        assert lr.fence_agent("ag") == 2
    assert st.reads("launch-receipt:") >= 1
    prefix_reads = [s for s in st.sql if "json_extract(value" in s and "key >=" in s]
    assert len(prefix_reads) == 1, "one range read of the receipt prefix, never per row"
    writes = [s for s in st.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    # per fenced receipt: counter increment, receipt write, old pointer delete, new pointer insert
    assert len(writes) == 2 * 4
    plan = db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT key, value FROM settings WHERE key >= ? AND key < ?",
        ("a", "b")).fetchall()
    assert any("USING INDEX" in str(tuple(row)) or "PRIMARY KEY" in str(tuple(row)) for row in plan)


# --- C8 message id forms; C9 cursor ---------------------------------------------------------

def test_c8_message_ids_use_the_canonical_helper_and_are_never_double_prefixed():
    _pending(trace="ta", cid="abc")
    _pending(trace="tb", cid="u-abc2")
    ids = [u["message_id"] for u in lr.delivery_page("ag", since=0, bootstrap=True)["updates"]]
    assert ids == ["u-abc", "u-abc2"]


def test_c9_cursor_pages_by_seq_with_cap_continuation_and_no_skip():
    for i in range(5):
        _pending(trace=f"t{i}", cid=f"c{i}")
    page = _normal(since=0, cap=2)
    assert ([u["delivery"]["seq"] for u in page["updates"]], page["cursor"], page["more"]) == ([1, 2], 2, True)
    page = _normal(since=2, cap=2)
    assert [u["delivery"]["seq"] for u in page["updates"]] == [3, 4] and page["more"]
    page = _normal(since=4, cap=2)
    assert ([u["delivery"]["seq"] for u in page["updates"]], page["cursor"], page["more"]) == ([5], 5, False)
    empty = _normal(since=5, cap=2)
    assert (empty["updates"], empty["cursor"], empty["more"]) == ([], 5, False)


def test_c9_exact_cap_gives_an_empty_follow_up():
    for i in range(2):
        _pending(trace=f"t{i}", cid=f"c{i}")
    page = _normal(since=0, cap=2)
    assert page["more"] and page["cursor"] == 2
    follow = _normal(since=2, cap=2)
    assert (follow["updates"], follow["more"], follow["cursor"]) == ([], False, 2)


def test_c9_other_agents_gaps_are_not_expiry_and_cursor_ahead_resets():
    _pending(agent="x", trace="tx", cid="cx")
    _pending(trace="t1", cid="c1")
    page = _normal(since=0)
    assert [u["delivery"]["seq"] for u in page["updates"]] == [2] and page["cursor"] == 2
    assert _normal(since=1)["expired"] is False
    assert _normal(since=99)["expired"] is True


def test_c9_a_raw_page_that_is_all_non_projectable_still_advances():
    _pending(trace="t0", cid="c0")
    _pending(trace="t1", cid="c1", gen=0)
    lr.record_pending(agent_id="ag", trace_id="t2", client_msg_id="c1", origin="user",
                      supersedes="t1")                     # t1 -> relaunched (not projected)
    settings_store.set_text(lr.pointer_key("ag", 50), "ghost")    # unverifiable pointer
    settings_store.set_int(lr.SEQ_KEY, 50)
    page = _normal(since=3, cap=5)
    assert page["updates"] == [] or all(u["delivery"]["for"]["trace_id"] != "t1" for u in page["updates"])
    assert page["skipped"] >= 1 and page["cursor"] == 50 and page["more"] is False


# --- C9b bootstrap with a fixed ceiling -------------------------------------------------------

def _fenced_pair_with_watermark():
    _pending(trace="f1", cid="cf1")
    _pending(trace="f2", cid="cf2")
    turn_queue.set_paused("ag", True, user_stop=True)
    lr.fence_agent("ag")
    settings_store.set_int(lr.PRUNE_SEQ_KEY, 100)
    settings_store.set_int(lr.SEQ_KEY, 150)


def test_c9b_i_fenced_pair_prune_100_cap_1_completes_without_a_loop():
    _fenced_pair_with_watermark()
    seen, pages = [], 0
    page = lr.delivery_page("ag", since=0, bootstrap=True, cap=1)
    until = page["until"]
    assert until == 150
    while True:
        pages += 1
        assert not page["expired"]
        seen += [u["delivery"]["for"]["trace_id"] for u in page["updates"]]
        if not page["more"]:
            break
        page = lr.delivery_page("ag", since=page["cursor"], until=until, bootstrap=True, cap=1,
                                stop_generation=_gen())
        assert pages < 10
    assert page["cursor"] == 150 and sorted(seen) == ["f1", "f2"]
    assert _normal(since=150)["expired"] is False, "normal mode from B"
    assert _normal(since=1)["expired"] is True, "(vii) normal mode unchanged"


def test_c9b_ii_writes_after_every_page_cannot_extend_the_pass():
    for i in range(5):
        _pending(trace=f"t{i}", cid=f"c{i}")
    page = lr.delivery_page("ag", since=0, bootstrap=True, cap=1)
    until, pages, moved = page["until"], 1, 0
    while page["more"]:
        lr.mark_attempted(f"t{4 - moved}")      # a receipt still ahead of the cursor moves past B
        lr.record_pending(agent_id="ag", trace_id=f"n{pages}", client_msg_id=f"n{pages}",
                          origin="user")         # and new work arrives
        moved = min(moved + 1, 4)
        page = lr.delivery_page("ag", since=page["cursor"], until=until, bootstrap=True, cap=1,
                                stop_generation=_gen())
        pages += 1
        assert pages <= 5 + 1, "ceil(R_B / cap) + 1"
    assert page["cursor"] == until
    after = _normal(since=until, cap=100)
    states = {u["delivery"]["for"]["trace_id"]: u["delivery"]["state"] for u in after["updates"]}
    assert any(s == "handed_over" for s in states.values()) and any(t.startswith("n") for t in states)


def test_c9b_iii_vi_validation_interruption_and_empty():
    assert lr.delivery_page("ag", since=0, bootstrap=True)["until"] == 0      # (vi) R_B = 0
    first = lr.delivery_page("ag", since=0, bootstrap=True)
    assert (first["updates"], first["more"], first["cursor"]) == ([], False, 0)
    _pending()
    settings_store.set_int(lr.SEQ_KEY, 10)
    g = _gen()
    assert lr.delivery_page("ag", since=5, until=3, stop_generation=g, bootstrap=True)["expired"]
    assert lr.delivery_page("ag", since=1, until=99, stop_generation=g, bootstrap=True)["expired"]
    assert lr.delivery_page("ag", since=1, stop_generation=g, bootstrap=True)["expired"]   # no B
    assert lr.delivery_page("ag", since=0, until=10, bootstrap=True)["expired"]            # no G
    resumed = lr.delivery_page("ag", since=0, until=10, stop_generation=g, bootstrap=True, cap=1)
    assert resumed["cursor"] == 1 and resumed["more"]


def test_c9b_iv_a_prune_between_pages_neither_errors_nor_expires():
    _pending(trace="old", cid="co")
    _pending(trace="keep", cid="ck")
    page = lr.delivery_page("ag", since=0, bootstrap=True, cap=1)
    settings_store.conn().execute("UPDATE settings SET updated_at = 0 WHERE key = ?",
                                  (lr.PREFIX + "keep",))
    lr.prune(lr.RETENTION_MS + 10)
    nxt = lr.delivery_page("ag", since=page["cursor"], until=page["until"], bootstrap=True, cap=1,
                           stop_generation=page["stop_generation"])
    assert not nxt["expired"] and nxt["updates"] == [] and not nxt["more"]


# --- C10 prune and the watermark ------------------------------------------------------------

def _age(key):
    settings_store.conn().execute("UPDATE settings SET updated_at = 0 WHERE key = ?", (key,))


def test_c10_retained_fenced_low_seq_cannot_hide_a_pruned_middle_hole():
    _pending(trace="f", cid="cf")
    turn_queue.set_paused("ag", True, user_stop=True)
    lr.fence_agent("ag")                                  # fenced, kept forever
    turn_queue.set_paused("ag", False)
    for i in range(3):
        _pending(trace=f"p{i}", cid=f"cp{i}")
    doomed = lr.get("p2")["seq"]
    _age(lr.PREFIX + "p2")
    _age(lr.PREFIX + "f")
    result = lr.prune(lr.RETENTION_MS + 10)
    assert result["receipts"] == 1 and lr.get("f") is not None
    assert settings_store.get_int(lr.PRUNE_SEQ_KEY) == doomed
    assert _normal(since=doomed - 1)["expired"] is True


def test_c10_a_seq_less_legacy_prune_leaves_the_watermark_unchanged():
    _legacy()
    _age(lr.PREFIX + "tl")
    assert lr.prune(lr.RETENTION_MS + 10)["receipts"] == 1
    assert settings_store.get_int(lr.PRUNE_SEQ_KEY) == 0


def test_c10_a_failed_prune_deletes_nothing_and_moves_no_watermark(monkeypatch):
    _pending()
    _age(lr.PREFIX + "t1")
    monkeypatch.setattr(settings_store, "set_int",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError):
        lr.prune(lr.RETENTION_MS + 10)
    monkeypatch.undo()
    assert lr.get("t1") is not None and _pointers() and settings_store.get_int(lr.PRUNE_SEQ_KEY) == 0


def test_c10_a_transition_after_a_prune_gets_a_seq_above_the_watermark():
    _pending(trace="a", cid="ca")
    _pending(trace="b", cid="cb")
    _age(lr.PREFIX + "a")
    lr.prune(lr.RETENTION_MS + 10)
    lr.mark_attempted("b")
    assert lr.get("b")["seq"] > settings_store.get_int(lr.PRUNE_SEQ_KEY)


def test_c10_prune_bounds_its_work_to_the_limit(monkeypatch):
    monkeypatch.setattr(lr, "PRUNE_LIMIT", 2)
    for i in range(5):
        _pending(trace=f"t{i}", cid=f"c{i}")
        _age(lr.PREFIX + f"t{i}")
    assert lr.prune(lr.RETENTION_MS + 10)["receipts"] == 2


# --- Addendum 1: the Stop-projection healing edge (generation-epoch cursor) --------------

def test_e_case_f_a_failed_fence_resets_a_cursor_taken_under_the_old_generation(monkeypatch):
    """Avana's case: T pending (rev 1); the client's cursor is past it under G0;
    the user's Stop commits G1; every fence write fails; no SSE, no transcript
    change. The normal request with G0 resets; the bounded bootstrap under G1
    shows T stopped at the SAME rev/seq; the final cursor (B, G1) then stays
    valid although the fence keeps failing."""
    t = _pending(trace="T", cid="cT")
    first = lr.delivery_page("ag", since=0, bootstrap=True)
    cursor, g0 = first["cursor"], first["stop_generation"]
    assert [u["delivery"]["state"] for u in first["updates"]] == ["pending_launch"]
    monkeypatch.setattr(lr, "fence_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    turn_queue.set_paused("ag", True, user_stop=True)
    assert lr.get("T")["state"] == "pending" and (lr.get("T")["rev"], lr.get("T")["seq"]) == (t["rev"], t["seq"])
    stale = lr.delivery_page("ag", since=cursor, stop_generation=g0)
    assert stale["expired"] and stale["updates"] == [] and stale["cursor"] is None
    boot = lr.delivery_page("ag", since=0, bootstrap=True)
    assert boot["stop_generation"] == g0 + 1
    (item,) = boot["updates"]
    assert (item["delivery"]["state"], item["delivery"]["rev"], item["delivery"]["seq"]) == \
        ("stopped", t["rev"], t["seq"])
    after = lr.delivery_page("ag", since=boot["cursor"], stop_generation=boot["stop_generation"])
    assert not after["expired"] and after["updates"] == [], "one reset only while G is stable"


def test_e_a_stop_between_bootstrap_pages_resets_the_pass():
    for i in range(3):
        _pending(trace=f"t{i}", cid=f"c{i}")
    page = lr.delivery_page("ag", since=0, bootstrap=True, cap=1)
    turn_queue.set_paused("ag", True, user_stop=True)
    nxt = lr.delivery_page("ag", since=page["cursor"], until=page["until"], bootstrap=True,
                           cap=1, stop_generation=page["stop_generation"])
    assert nxt["expired"] and nxt["updates"] == []


def test_e_missing_future_or_old_generation_is_expired():
    _pending()
    g = _gen()
    for bad in (None, g + 1, g - 1):
        assert lr.delivery_page("ag", since=1, stop_generation=bad)["expired"]
    assert lr.delivery_page("ag", since=1, stop_generation=g)["expired"] is False


def test_e_a_maintenance_cancel_never_resets_cursors():
    _pending()
    g = _gen()
    turn_queue.set_paused("ag", True)
    assert lr.delivery_page("ag", since=1, stop_generation=g)["expired"] is False


# --- Dispatch-level: S1, S2, accounting, mixed version ------------------------------------

class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)


def _service(tmp_path):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                      session="mike", backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    stream = SimpleNamespace(events=[], broadcast=None)
    stream.broadcast = stream.events.append
    ctx = SimpleNamespace(default_session="mike", agents_path=tmp_path / "unused.json", stream=stream)
    return TurnDispatchService(ctx, backend_registry=_Backends(), home=tmp_path,
                               uuid_factory=lambda: "bs-1", retry_scheduler=lambda *_: None), agent_id


def _send(service, cid, trace):
    from lib.turn_dispatch import DispatchCommand
    return service.submit(DispatchCommand(text="hi", requested_session="mike", trace_id=trace,
                                          client_msg_id=cid, synthesize_audio=False, origin="user"))


def test_s1_on_a_launched_send_is_handed_over_and_never_stopped(tmp_path):
    service, agent = _service(tmp_path)
    result = _send(service, "u-s1", "ts1")
    assert result.delivery["state"] == "handed_over"
    assert result.delivery["for"] == {"client_msg_id": "u-s1", "trace_id": "ts1"}
    receipt = lr.get("ts1")
    assert lr.delivery_for_send("u-s1", agent_id=agent, origin="user", sender_agent_id="") == \
        lr.project(receipt, agent_id=agent, user_stop_generation=0)
    settings_store.set_text(lr.PREFIX + "ts1", json.dumps(dict(receipt, state="fenced")))
    assert lr.delivery_for_send("u-s1", agent_id=agent, origin="user", sender_agent_id="") is None


def test_s2_annotation_uses_one_batched_read_and_one_generation_read(tmp_path):
    service, agent = _service(tmp_path)
    for i in range(3):
        _send(service, f"u-r{i}", f"tr{i}")
        _td.reset_for_tests()
    turns = [{"id": f"u-r{i}", "role": "user", "trace_id": f"tr{i}", "origin": "user",
              "sender_agent_id": ""} for i in range(3)] + [{"id": "a", "role": "assistant"}]
    with _Statements() as st:
        assert lr.annotate_rows(agent, turns) == 0
    assert all(t["delivery"]["state"] == "handed_over" for t in turns[:3])
    assert "delivery" not in turns[3]
    assert st.reads("launch-receipt:") == 1 and st.reads("turn_queue.user_stop_generation") == 1


def test_c12_a_feed_page_does_constant_reads_regardless_of_retained_volume(tmp_path):
    for i in range(40):
        _pending(trace=f"t{i}", cid=f"c{i}")
    for i in range(40):
        _pending(agent="other", trace=f"o{i}", cid=f"o{i}")
    g = _gen()
    with _Statements() as st:
        lr.delivery_page("ag", since=30, cap=5, stop_generation=g)
    selects = [s for s in st.sql if s.lstrip().upper().startswith("SELECT")]
    # counter, watermark, pointer range, get_many, user stop generation
    assert len(selects) == 5
    assert not any("json_extract" in s for s in selects), "no receipt-prefix scan on a feed page"


def test_mixed_version_old_runtime_result_stays_unknown():
    from lib.runtime_bridge import RuntimeClient
    old = RuntimeClient._dispatch_result({"ok": True, "result": {"session": "mike", "backend": "c"}})
    new = RuntimeClient._dispatch_result({"ok": True, "result": {
        "session": "mike", "backend": "c", "delivery": {"state": "handed_over"}}})
    junk = RuntimeClient._dispatch_result({"ok": True, "result": {
        "session": "mike", "backend": "c", "delivery": "handed_over"}})
    assert (old.delivery, new.delivery["state"], junk.delivery) == (None, "handed_over", None)


def test_the_feature_is_implemented_but_not_advertised_until_allocated():
    from lib import server_identity
    assert server_identity.SEND_DELIVERY_STATE not in server_identity.FEATURES
    assert server_identity.SEND_DELIVERY_STATE not in server_identity.FEATURE_CONTRACTS
    assert server_identity.feature_advertised(server_identity.SEND_DELIVERY_STATE) is False


def test_no_hint_is_emitted_while_the_feature_is_not_advertised(tmp_path):
    service, _agent = _service(tmp_path)
    _send(service, "u-h", "th")
    assert not any(getattr(e, "type", None) == "delivery-updated"
                   or "delivery-updated" in str(e) for e in service.ctx.stream.events)


def test_hints_follow_committed_transitions_when_advertised(tmp_path, monkeypatch):
    from lib import server_identity
    monkeypatch.setattr(server_identity, "feature_advertised", lambda name: True)
    service, _agent = _service(tmp_path)
    _send(service, "u-h2", "th2")
    hints = [e for e in service.ctx.stream.events if "delivery-updated" in str(e)]
    assert len(hints) >= 2, "pending at admission, then handed_over at the handoff"

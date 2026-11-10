"""10 Nov 2026: on a long-lived Host GET /agents/snapshot carried 1,375 agents (2.9 MB), 1,019
of them archived, on every client connect. A client can now ask for the snapshot without the
archived agents, which are then not built at all, and page those separately. A client that
does not ask gets what it always got."""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import backends, db, reconcile, snapshot

FULL_KEYS = {"agents", "focus", "roster", "personas", "model_avatars", "available_mcp_servers",
             "tool_explanations"}


@pytest.fixture(autouse=True)
def _no_live_work(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])


def _agent(session: str, *, parent: str | None = None, archived_at: int | None = None,
           state: str = "done", helper_state: str | None = None) -> str:
    agent_id = agents_db.create_agent(persona=session.title(), voice_id="V", cwd="/tmp",
                                      session=session, backend="codex")
    agents_db.start_runtime(agent_id, session)
    agents_db.record_state(agent_id, state, {"origin": "user", "trace_id": "t-" + session})
    con = db.conn()
    if parent:
        con.execute("UPDATE agents SET parent_agent_id=?, role='helper', helper_state=? WHERE agent_id=?",
                    (parent, helper_state or "done", agent_id))
    if archived_at is not None:
        con.execute("UPDATE agents SET archived_at=? WHERE agent_id=?", (archived_at, agent_id))
    return agent_id


@pytest.fixture
def fleet() -> dict[str, str]:
    """Two agents in use, one of them with helpers; archived helpers and an archived agent."""
    ids = {"lead": _agent("lead"), "solo": _agent("solo")}
    ids["running"] = _agent("helper-running", parent=ids["lead"], state="waiting", helper_state="running")
    for n in range(7):                                    # archived at 1000, 1001, ... 1006
        ids[f"old{n}"] = _agent(f"helper-old-{n}", parent=ids["lead"], archived_at=1000 + n)
    ids["retired"] = _agent("retired", archived_at=2000)  # an ordinary agent, archived last
    return ids


def _archived_ids(fleet) -> list[str]:
    """Most recently archived first."""
    return [fleet["retired"], *(fleet[f"old{n}"] for n in reversed(range(7)))]


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["agent_id"]: row for row in rows}


def _pages(limit: int) -> list[dict]:
    pages, cursor = [], ""
    while True:
        page = snapshot.build_archived_agents(None, limit=limit, cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


# --- a client that does not ask ------------------------------------------------------

def test_the_default_snapshot_still_carries_every_agent_and_no_new_key(fleet):
    default = snapshot.build_agent_snapshot(None)
    assert set(default) == FULL_KEYS                       # exactly the keys it had; no archived_count
    assert {row["agent_id"] for row in default["agents"]} == set(fleet.values())
    assert sum(1 for row in default["agents"] if row["archived_at"]) == 8
    # Saying so explicitly is the same request.
    assert json.dumps(snapshot.build_agent_snapshot(None, include_archived=True)) == json.dumps(default)


# --- a client that asks -----------------------------------------------------------------

def test_without_archived_agents_the_rest_is_the_same_snapshot(fleet):
    default = snapshot.build_agent_snapshot(None)
    lean = snapshot.build_agent_snapshot(None, include_archived=False)

    assert [row["agent_id"] for row in lean["agents"]] == [
        row["agent_id"] for row in default["agents"] if not row["archived_at"]]
    assert {row["session"] for row in lean["agents"]} == {"lead", "solo", "helper-running"}
    # Each remaining row is the row the full snapshot has, byte for byte.
    full_rows = _by_id(default["agents"])
    for row in lean["agents"]:
        assert json.dumps(row) == json.dumps(full_rows[row["agent_id"]])
    # Every other key is the same; one key is added.
    assert set(lean) == FULL_KEYS | {"archived_count"}
    assert lean["archived_count"] == 8
    for key in FULL_KEYS - {"agents"}:
        assert json.dumps(lean[key]) == json.dumps(default[key]), key
    assert len(json.dumps(lean["agents"])) < len(json.dumps(default["agents"])) / 3   # 3 rows of 11


def test_a_parent_still_counts_its_archived_helpers(fleet):
    lead = _by_id(snapshot.build_agent_snapshot(None, include_archived=False)["agents"])[fleet["lead"]]
    assert (lead["child_count"], lead["running_children"]) == (8, 1)   # seven of the eight are archived
    assert lead["background_jobs"]["sub_agents"] == 1


def test_the_roster_keeps_the_names_of_archived_agents(fleet):
    lean = snapshot.build_agent_snapshot(None, include_archived=False)
    assert "Retired" in lean["roster"] and "Helper-Old-3" in lean["roster"]
    assert lean["roster"] == snapshot.build_agent_snapshot(None)["roster"]


# --- the archived agents ------------------------------------------------------------------

def test_the_archived_listing_is_exactly_what_was_left_out(fleet):
    default = snapshot.build_agent_snapshot(None)
    lean = snapshot.build_agent_snapshot(None, include_archived=False)
    pages = _pages(limit=3)

    assert [len(page["agents"]) for page in pages] == [3, 3, 2]
    assert all(page["total"] == 8 for page in pages)
    listed = [row for page in pages for row in page["agents"]]
    assert [row["agent_id"] for row in listed] == _archived_ids(fleet)      # most recently archived first
    # The rows are the full snapshot's rows.
    full_rows = _by_id(default["agents"])
    for row in listed:
        assert json.dumps(row) == json.dumps(full_rows[row["agent_id"]])
    # No agent is in both, and none is in neither.
    lean_ids, listed_ids = {row["agent_id"] for row in lean["agents"]}, {row["agent_id"] for row in listed}
    assert not lean_ids & listed_ids
    assert lean_ids | listed_ids == {row["agent_id"] for row in default["agents"]} == set(fleet.values())
    assert len(listed) == lean["archived_count"]


def test_one_page_holds_everything_when_it_fits(fleet):
    page = snapshot.build_archived_agents(None)
    assert len(page["agents"]) == page["total"] == 8 and page["next_cursor"] is None


def test_nothing_archived_is_an_empty_last_page():
    _agent("only")
    assert snapshot.build_archived_agents(None) == {"agents": [], "next_cursor": None, "total": 0}
    assert snapshot.build_agent_snapshot(None, include_archived=False)["archived_count"] == 0


def test_archiving_and_restoring_between_pages_neither_repeats_nor_hides_an_agent(fleet):
    first = snapshot.build_archived_agents(None, limit=3)
    seen = [row["agent_id"] for row in first["agents"]]
    assert seen == _archived_ids(fleet)[:3]
    # Between the pages: the newest one is restored, and another agent is archived just now.
    db.conn().execute("UPDATE agents SET archived_at=NULL WHERE agent_id=?", (fleet["retired"],))
    db.conn().execute("UPDATE agents SET archived_at=9000 WHERE agent_id=?", (fleet["solo"],))

    cursor = first["next_cursor"]
    while cursor is not None:
        page = snapshot.build_archived_agents(None, limit=3, cursor=cursor)
        seen += [row["agent_id"] for row in page["agents"]]
        cursor = page["next_cursor"]

    assert seen == _archived_ids(fleet)                    # every agent of the first walk, once, in order
    assert fleet["solo"] not in seen                       # archived after the walk began: on the next walk
    assert snapshot.build_archived_agents(None, limit=1)["agents"][0]["agent_id"] == fleet["solo"]


def test_agents_archived_in_the_same_millisecond_page_in_a_fixed_order():
    ids = sorted((_agent(f"twin-{n}", archived_at=5000) for n in range(5)), reverse=True)
    listed = [row["agent_id"] for page in _pages(limit=2) for row in page["agents"]]
    assert listed == ids


@pytest.mark.parametrize("limit, expected", [(0, 1), (-5, 1), (1, 1), (200, 8), (10_000, 8)])
def test_the_page_size_is_held_between_one_and_the_maximum(fleet, limit, expected):
    assert len(snapshot.build_archived_agents(None, limit=limit)["agents"]) == expected
    assert snapshot.ARCHIVED_PAGE_MAX == 200 and snapshot.ARCHIVED_PAGE_DEFAULT == 50


@pytest.mark.parametrize("cursor", ["nonsense", "e30=", "WzEsMl0=", "WyJ4IiwieSJd", "W3RydWUsIngiXQ==", "x" * 600])
def test_a_cursor_this_host_did_not_issue_is_refused(fleet, cursor):
    with pytest.raises(ValueError):
        snapshot.build_archived_agents(None, cursor=cursor)


# --- the Host does not pay for what it leaves out -----------------------------------------------

def _statements(build) -> list[str]:
    build()                                               # the first build of a process seeds and caches
    seen: list[str] = []
    con = db.conn()
    con.set_trace_callback(seen.append)
    try:
        build()
    finally:
        con.set_trace_callback(None)
    return seen


def _busy_archived(count: int) -> None:
    """Archived agents whose last recorded state is a busy one: each costs the full snapshot
    its own statements (the open-turn lookup of the status line, the stuck-busy check)."""
    for n in range(count):
        _agent(f"stuck-{n}", archived_at=3000 + n, state="thinking")


def test_archived_agents_cost_the_lean_snapshot_no_statement_of_their_own(fleet):
    lean_before = len(_statements(lambda: snapshot.build_agent_snapshot(None, include_archived=False)))
    full_before = len(_statements(lambda: snapshot.build_agent_snapshot(None)))

    _busy_archived(40)

    lean_after = len(_statements(lambda: snapshot.build_agent_snapshot(None, include_archived=False)))
    full_after = len(_statements(lambda: snapshot.build_agent_snapshot(None)))
    # Forty more archived agents: nothing. (A couple of fleet-wide reads are cached by time.)
    assert abs(lean_after - lean_before) <= 3, (lean_before, lean_after)
    assert full_after >= full_before + 40, (full_before, full_after)  # the full snapshot pays per agent


def test_only_the_agents_asked_for_are_reconciled(fleet, monkeypatch):
    reconciled: list[str] = []
    real = reconcile.reconcile_agent

    def record(agent_id, *args, **kwargs):
        reconciled.append(agent_id)
        return real(agent_id, *args, **kwargs)

    monkeypatch.setattr(reconcile, "reconcile_agent", record)
    archived = set(_archived_ids(fleet))

    snapshot.build_agent_snapshot(None, include_archived=False)
    assert set(reconciled) == set(fleet.values()) - archived and len(reconciled) == 3

    reconciled.clear()
    snapshot.build_archived_agents(None, limit=3)
    assert reconciled == _archived_ids(fleet)[:3]          # the page, nothing else

    reconciled.clear()
    snapshot.build_agent_snapshot(None)
    assert set(reconciled) == set(fleet.values())          # the default: every agent, as before


def test_a_page_of_archived_agents_costs_the_same_however_many_there_are(fleet):
    small = len(_statements(lambda: snapshot.build_archived_agents(None, limit=5)))
    _busy_archived(40)
    large = len(_statements(lambda: snapshot.build_archived_agents(None, limit=5)))
    # The five newest are now busy ones, each with its own statements; the other 43 cost nothing.
    assert large <= small + 5 * 4, (small, large)


# --- one build is shared by the requests that asked for the same thing ------------------------------

def test_the_two_bodies_are_shared_separately(fleet):
    shared = snapshot.SharedSnapshot()
    full = json.loads(shared.body(None))
    lean = json.loads(shared.body(None, include_archived=False))
    assert len(full["agents"]) == 11 and len(lean["agents"]) == 3
    assert "archived_count" in lean and "archived_count" not in full
    # Asking again for either gives that one, never the other.
    assert len(json.loads(shared.body(None))["agents"]) == 11
    assert len(json.loads(shared.body(None, include_archived=False))["agents"]) == 3
    assert shared.body(None) == json.dumps(snapshot.build_agent_snapshot(None)).encode()

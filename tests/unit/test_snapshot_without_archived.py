"""10 Nov 2026: on a long-lived Host GET /agents/snapshot carried 1,375 agents (2.9 MB), 1,019
of them archived, on every client connect. A client can now ask for the snapshot without the
archived agents, which are then not built at all, and page those separately. A client that
does not ask gets what it always got."""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import backends, db, message_previews, reconcile, snapshot

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
    """Every page of one walk. Bounded: a cursor that is ignored must fail, not spin."""
    pages, cursor = [], ""
    for _ in range(50):
        page = snapshot.build_archived_agents(None, limit=limit, cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("the walk did not end: the cursor does not advance")


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


def test_a_page_holds_at_most_the_maximum():
    for n in range(snapshot.ARCHIVED_PAGE_MAX + 5):
        _agent(f"many-{n:03d}", archived_at=1000 + n)
    assert (snapshot.ARCHIVED_PAGE_MAX, snapshot.ARCHIVED_PAGE_DEFAULT) == (200, 50)
    first = snapshot.build_archived_agents(None, limit=200)
    assert len(first["agents"]) == 200 and first["total"] == 205 and first["next_cursor"]
    rest = snapshot.build_archived_agents(None, limit=200, cursor=first["next_cursor"])
    assert len(rest["agents"]) == 5 and rest["next_cursor"] is None
    assert len(snapshot.build_archived_agents(None)["agents"]) == 50          # the default page
    assert len(snapshot.build_archived_agents(None, limit=1)["agents"]) == 1


@pytest.mark.parametrize("limit", [0, -5, 201, 10_000, True, 1.5, "5"])
def test_a_limit_outside_one_to_the_maximum_is_refused_not_clamped(fleet, limit):
    with pytest.raises(ValueError):
        snapshot.build_archived_agents(None, limit=limit)
    with pytest.raises(ValueError):
        snapshot.SharedSnapshot().archived_page(None, limit=limit)


def test_the_query_is_parsed_strictly():
    parse = snapshot.parse_archived_query
    assert parse("", "") == (50, None, "")                  # no value: the default page from the start
    assert parse("1", "")[0] == 1 and parse("7", "")[0] == 7 and parse("200", "")[0] == 200
    for bad in ("0", "201", "1000", "-1", "+5", " 5 ", "5 ", "1_0", "\u0665", "\uff15", "5.0", "0x5", "five"):
        with pytest.raises(ValueError):
            parse(bad, "")
    with pytest.raises(ValueError):
        parse("5", "nonsense")


def _cursor(*parts) -> str:
    import base64
    return base64.urlsafe_b64encode(json.dumps(list(parts)).encode()).decode()


@pytest.mark.parametrize("cursor", [
    "nonsense", "e30=", "x" * 600,
    _cursor(1, "a"),                    # the first shape of this cursor, without a listing token
    _cursor("x", "y", ""), _cursor(True, "x", ""), _cursor(1, 2, ""), _cursor(1, "a", 3), _cursor(1, "a", "", "extra"),
])
def test_a_cursor_this_host_did_not_issue_is_refused(fleet, cursor):
    with pytest.raises(ValueError):
        snapshot.build_archived_agents(None, cursor=cursor)
    with pytest.raises(ValueError):
        snapshot.parse_archived_query("", cursor)


def test_a_cursor_from_a_listing_that_is_gone_continues_from_its_agent(fleet):
    """A Host restart, or a walk slower than the listing is held: the token names nothing."""
    position = (1003, fleet["old3"])
    page = snapshot.build_archived_agents(None, limit=10, cursor=_cursor(*position, "a-listing-long-gone"))
    assert [row["agent_id"] for row in page["agents"]] == [fleet["old2"], fleet["old1"], fleet["old0"]]
    shared = snapshot.SharedSnapshot().archived_page(None, limit=10, position=position, token="a-listing-long-gone")
    assert [row["agent_id"] for row in shared["agents"]] == [row["agent_id"] for row in page["agents"]]


def test_an_archive_time_that_is_not_a_number_does_not_break_the_listing(fleet):
    """The snapshot answers such a row as it is; the listing must not turn it into an error."""
    db.conn().execute("UPDATE agents SET archived_at='yesterday' WHERE agent_id=?", (fleet["old4"],))
    full = _by_id(snapshot.build_agent_snapshot(None)["agents"])
    assert full[fleet["old4"]]["archived_at"] == "yesterday"
    listed = [row for page in _pages(limit=3) for row in page["agents"]]
    assert len(listed) == 8 and listed[-1]["agent_id"] == fleet["old4"]        # still archived; it sorts last
    assert json.dumps(listed[-1]) == json.dumps(full[fleet["old4"]])
    assert fleet["old4"] not in _by_id(snapshot.build_agent_snapshot(None, include_archived=False)["agents"])


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


def _more_archived(count: int, *, state: str, prefix: str) -> None:
    for n in range(count):
        _agent(f"{prefix}-{n}", archived_at=3000 + n, state=state)


def test_idle_archived_agents_cost_the_lean_snapshot_no_statement_of_their_own(fleet):
    lean = lambda: snapshot.build_agent_snapshot(None, include_archived=False)
    before = len(_statements(lean))
    _more_archived(40, state="done", prefix="idle")
    after = len(_statements(lean))
    # Forty more archived agents: nothing. (A couple of fleet-wide reads are cached by time.)
    assert abs(after - before) <= 3, (before, after)
    assert len(lean()["agents"]) == 3 and lean()["archived_count"] == 48


def test_an_archived_agent_that_looks_active_is_reconciled_but_not_built(fleet, monkeypatch):
    """Archiving does not stop a turn. If it dies afterwards and no client asks for
    archived agents any more, only this keeps the agent from staying busy for ever."""
    stuck = _agent("stuck", archived_at=3000, state="thinking")
    handled = _agent("handled", archived_at=3001)
    with monkeypatch.context() as m:
        m.setattr(backends, "active_handles", lambda backend, agent_id: ["h"] if agent_id == handled else [])
        reconciled: list[str] = []
        m.setattr(reconcile, "reconcile_agent", lambda agent_id, *a, **kw: reconciled.append(agent_id) or {})
        lean = snapshot.build_agent_snapshot(None, include_archived=False)
    listed = {row["agent_id"] for row in lean["agents"]}
    assert stuck not in listed and handled not in listed and lean["archived_count"] == 10
    assert sorted(reconciled) == sorted([*listed, stuck, handled])     # the idle archived eight: not touched

    # It costs the lean build a bounded amount per such agent, never a row.
    lean_build = lambda: snapshot.build_agent_snapshot(None, include_archived=False)
    before = len(_statements(lean_build))
    _more_archived(40, state="thinking", prefix="busy")
    after = len(_statements(lean_build))
    assert 0 < after - before <= 3 * 40, (before, after)
    assert len(lean_build()["agents"]) == 3


def test_with_the_runtime_unreachable_nothing_is_reconciled_blind(fleet, monkeypatch):
    _agent("stuck", archived_at=3000, state="thinking")

    class Down:
        def status(self):
            raise OSError("runtime down")

        def status_within(self, total_s):
            raise OSError("runtime down")

    reconciled: list[str] = []
    monkeypatch.setattr(reconcile, "reconcile_agent", lambda agent_id, *a, **kw: reconciled.append(agent_id) or {})
    backends.configure_runtime_client(Down())
    try:
        snapshot.build_agent_snapshot(None, include_archived=False)
    finally:
        backends.configure_runtime_client(None)
    assert reconciled == []


def test_each_build_reconciles_the_agents_it_builds(fleet, monkeypatch):
    reconciled: list[str] = []
    real = reconcile.reconcile_agent

    def record(agent_id, *args, **kwargs):
        reconciled.append(agent_id)
        return real(agent_id, *args, **kwargs)

    monkeypatch.setattr(reconcile, "reconcile_agent", record)
    archived = _archived_ids(fleet)

    snapshot.build_agent_snapshot(None, include_archived=False)
    assert set(reconciled) == set(fleet.values()) - set(archived) and len(reconciled) == 3

    reconciled.clear()
    snapshot.build_archived_agents(None, limit=3)
    assert reconciled == archived                         # the whole listing, once, whatever the page size

    reconciled.clear()
    snapshot.build_agent_snapshot(None)
    assert set(reconciled) == set(fleet.values())          # the default: every agent, as before


# --- message previews: three statements each when the cache cannot answer ------------------------

def _with_messages(fleet) -> None:
    db.conn().executemany(
        """INSERT INTO messages (message_id,agent_id,backend_session_id,seq,role,text,revision,updated_at)
           VALUES (?,?,?,1,'user','hello',1,1000)""",
        [(f"m-{agent_id}", agent_id, f"s-{agent_id}") for agent_id in fleet.values()])


def _previews_computed(monkeypatch) -> list[str]:
    computed: list[str] = []
    real = message_previews._agent_preview

    def record(agent_id, *args):
        computed.append(agent_id)
        return real(agent_id, *args)

    monkeypatch.setattr(message_previews, "_agent_preview", record)
    return computed


def _cold_previews() -> None:
    message_previews._PREVIEW_CACHE.reset()
    message_previews._REVISION_CACHE.reset()


def test_a_cold_preview_cache_costs_the_lean_snapshot_nothing_for_archived_agents(fleet, monkeypatch):
    """On the Host a snapshot that could not use the preview cache ran 4,181 statements,
    2,750 of them one agent's message head at a time, archived agents included."""
    _with_messages(fleet)
    computed = _previews_computed(monkeypatch)
    archived = set(_archived_ids(fleet))

    _cold_previews()
    lean = snapshot.build_agent_snapshot(None, include_archived=False)
    assert set(computed) == set(fleet.values()) - archived and len(computed) == 3
    assert all(row["last_message"] == "You: hello" for row in lean["agents"])

    _cold_previews()
    computed.clear()
    page = snapshot.build_archived_agents(None, limit=3)
    assert sorted(computed) == sorted(archived)            # the archived listing: the archived agents, no others
    assert all(row["last_message"] == "You: hello" for row in page["agents"])

    _cold_previews()
    computed.clear()
    snapshot.build_agent_snapshot(None)
    assert set(computed) == set(fleet.values())            # the default: every agent, as before


def test_a_lean_snapshot_leaves_the_other_agents_cached_previews_alone(fleet, monkeypatch):
    _with_messages(fleet)
    _cold_previews()
    full = snapshot.build_agent_snapshot(None)             # fills the cache for every agent
    computed = _previews_computed(monkeypatch)

    snapshot.build_agent_snapshot(None, include_archived=False)
    snapshot.build_archived_agents(None, limit=2)
    assert computed == []                                  # both read the cache

    again = snapshot.build_agent_snapshot(None)
    assert computed == [], "a partial projection must not evict what it did not ask for"
    assert json.dumps(again) == json.dumps(full)


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

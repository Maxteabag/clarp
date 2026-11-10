"""Concurrent /agents/snapshot requests share one build without going stale."""
import json
import threading
import time
import types

import pytest

from lib import snapshot


def test_requests_waiting_on_a_build_share_the_next_one(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    builds = []

    def build(ctx):
        builds.append(ctx)
        if len(builds) == 1:
            first_started.set()
            release_first.wait(5)
        return {"build": len(builds)}

    monkeypatch.setattr(snapshot, "build_agent_snapshot", build)
    clock_reads = threading.Semaphore(0)

    def monotonic():
        clock_reads.release()
        return time.monotonic()

    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=monotonic))
    shared = snapshot.SharedSnapshot()
    ctx = object()
    bodies = []
    first = threading.Thread(target=lambda: bodies.append(shared.body(ctx)))
    first.start()
    assert first_started.wait(5)
    # Ten clients arrive while the first build runs. The running build may
    # predate their writes, so they share exactly one build that starts later.
    waiters = [threading.Thread(target=lambda: bodies.append(shared.body(ctx))) for _ in range(10)]
    for waiter in waiters:
        waiter.start()
    # Every request reads the clock on arrival; the first also when it starts
    # building. Release only once all ten have arrived.
    for _ in range(2 + len(waiters)):
        assert clock_reads.acquire(timeout=5)
    release_first.set()
    for thread in [first, *waiters]:
        thread.join(5)
    assert len(builds) == 2
    assert sorted(bodies) == [b'{"build": 1}'] + [b'{"build": 2}'] * 10


def test_a_request_after_a_finished_build_gets_a_fresh_one(monkeypatch):
    builds = []
    monkeypatch.setattr(snapshot, "build_agent_snapshot",
                        lambda ctx: builds.append(ctx) or {"build": len(builds)})
    shared = snapshot.SharedSnapshot()
    ctx = object()
    assert shared.body(ctx) == b'{"build": 1}'
    assert shared.body(ctx) == b'{"build": 2}'


# --- three projections behind one lock (review of PR 252, findings 2 and 4) ----------------------
#
# The full snapshot, the one without archived agents and the archived listing are shared
# separately. A body is only ever reused by a request that waited behind a build, so calling
# them one after another proves nothing about the keys: these tests put waiters of every
# kind behind a running build and release them together.

def _behind_a_running_build(monkeypatch, first: str, waiters: list[str]):
    """Start one request of kind ``first`` and hold its build; let ``waiters`` arrive; release.
    Returns [(asked, got, build number)] per request, the kinds built in order, and the
    largest number of builds that ran at the same time."""
    first_started, release_first = threading.Event(), threading.Event()
    built: list[str] = []
    running, most = [0], [0]

    def build(kind):
        running[0] += 1
        most[0] = max(most[0], running[0])
        built.append(kind)
        number = len(built)
        if number == 1:
            first_started.set()
            assert release_first.wait(10)
        running[0] -= 1
        return number

    def build_snapshot(ctx, include_archived=True):
        kind = "full" if include_archived else "lean"
        return {"kind": kind, "build": build(kind)}

    def build_listing(ctx):
        number = build("archived")
        return [{"kind": "archived", "build": number}] * 3, [(3, "c"), (2, "b"), (1, "a")]

    monkeypatch.setattr(snapshot, "build_agent_snapshot", build_snapshot)
    monkeypatch.setattr(snapshot, "build_archived_listing", build_listing)
    clock_reads = threading.Semaphore(0)

    def monotonic():
        clock_reads.release()
        return time.monotonic()

    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=monotonic))
    shared, ctx, got = snapshot.SharedSnapshot(), object(), []

    def ask(kind):
        if kind == "archived":
            row = shared.archived_page(ctx, limit=1)["agents"][0]
        else:
            row = json.loads(shared.body(ctx, include_archived=kind == "full"))
        got.append((kind, row["kind"], row["build"]))

    threads = [threading.Thread(target=ask, args=(first,))]
    threads[0].start()
    assert first_started.wait(10)
    threads += [threading.Thread(target=ask, args=(kind,)) for kind in waiters]
    for thread in threads[1:]:
        thread.start()
    # Every request reads the clock on arrival; the first also when its build starts.
    for _ in range(2 + len(waiters)):
        assert clock_reads.acquire(timeout=10)
    release_first.set()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    return got, built, most[0]


@pytest.mark.parametrize("first", ["full", "lean", "archived"])
@pytest.mark.parametrize("waiters", [
    ["lean", "full"] * 5,
    ["full"] * 4 + ["lean"] * 4 + ["archived"] * 4,
    ["archived", "lean", "full", "full", "lean", "archived", "lean", "archived", "full"],
    ["lean"] * 6,
    ["full"] * 6,
    ["archived"] * 12,
], ids=["alternating", "grouped", "mixed", "all-lean", "all-full", "all-archived"])
def test_waiters_behind_a_running_build_get_what_they_asked_for(monkeypatch, first, waiters):
    got, built, most_at_once = _behind_a_running_build(monkeypatch, first, waiters)

    assert len(got) == 1 + len(waiters)
    assert [g for g in got if g[0] != g[1]] == [], "a request was answered with another kind's projection"
    assert most_at_once == 1, "two builds ran at the same time"
    # The first, then one per kind that was waited for: every waiter of a kind shares it.
    assert built[0] == first and sorted(built[1:]) == sorted(set(waiters))
    # No waiter is answered from the build that was already running when it arrived.
    numbers = sorted(number for asked, _kind, number in got)
    assert numbers.count(1) == 1


# --- the pages of the archived listing (round two of the review of PR 252, items 1 and 4) ---------
#
# A first page (no cursor) asks for a current listing and builds one. Every later page is cut
# from whichever listing is held, whoever's first page built it: a cursor is a place in the
# order, so the builds are one per first page, not one per page.

def _listings(monkeypatch, make):
    """Count the listing builds; ``make(number)`` is the archived agents of build ``number`` as
    [(archived_at, agent_id)], most recently archived first."""
    builds: list[int] = []

    def build(ctx):
        builds.append(len(builds) + 1)
        order = make(len(builds))
        return [{"agent_id": agent_id, "build": len(builds)} for _at, agent_id in order], order

    monkeypatch.setattr(snapshot, "build_archived_listing", build)
    return builds


def _next(shared, ctx, cursor: str, limit: int) -> dict:
    """One page the way the route asks for it: the cursor is all a client carries."""
    size, position = snapshot.parse_archived_query(str(limit), cursor)
    return shared.archived_page(ctx, limit=size, position=position)


def _fixed(count: int):
    return lambda number: [(1000 - n, f"agent-{n:03d}") for n in range(count)]


def test_the_pages_of_one_walk_are_cut_from_one_listing(monkeypatch):
    builds = _listings(monkeypatch, _fixed(7))
    shared, ctx = snapshot.SharedSnapshot(), object()

    seen, cursor = [], ""
    for _ in range(10):
        page = _next(shared, ctx, cursor, 3)
        seen += [(row["agent_id"], row["build"]) for row in page["agents"]]
        assert page["total"] == 7
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [(f"agent-{n:03d}", 1) for n in range(7)]
    assert builds == [1]                                  # three pages, one build

    # A first page is a current listing: it must see what was written since.
    again = _next(shared, ctx, "", 3)
    assert builds == [1, 2] and again["agents"][0]["build"] == 2
    # Another context never shares it.
    _next(shared, object(), again["next_cursor"], 3)
    assert builds == [1, 2, 3]


def test_two_clients_paging_at_once_cost_one_build_each_not_one_per_page(monkeypatch):
    """The reviewer's case: two clients walking 1,019 archived agents alternately were 42 page
    requests and 42 builds of every archived row, because a first page replaced the listing
    the other walk was reading and only the cursor of the held listing could reuse it."""
    builds = _listings(monkeypatch, _fixed(1019))
    shared, ctx = snapshot.SharedSnapshot(), object()
    walks = {"a": {"cursor": "", "seen": [], "answered_by": set()},
             "b": {"cursor": "", "seen": [], "answered_by": set()}}

    requests = 0
    while any(walk["cursor"] is not None for walk in walks.values()):
        for walk in walks.values():                       # a page of one, then a page of the other
            if walk["cursor"] is None:
                continue
            page = _next(shared, ctx, walk["cursor"], 50)
            requests += 1
            assert requests <= 42, "a walk did not end"
            walk["seen"] += [row["agent_id"] for row in page["agents"]]
            walk["answered_by"] |= {row["build"] for row in page["agents"]}
            walk["cursor"] = page["next_cursor"]

    assert requests == 42
    assert builds == [1, 2]                               # the two first pages; no other page built
    everyone = [f"agent-{n:03d}" for n in range(1019)]
    assert walks["a"]["seen"] == everyone and walks["b"]["seen"] == everyone
    # The first walk went on in the second walk's listing from where it was.
    assert walks["a"]["answered_by"] == {1, 2} and walks["b"]["answered_by"] == {2}


def test_a_client_refreshing_the_first_page_does_not_make_a_walk_build(monkeypatch):
    """One client walks; another asks for the first page between every two of its pages, and
    the archive changes under them: each first page is a build, no page of the walk is, and
    the walk neither repeats an agent nor loses one that stayed archived."""
    def archive(number):
        agents = [(1000 - n, f"agent-{n:02d}") for n in range(20)]
        # From the second listing on: one agent the walk has passed and one it has not reached
        # are restored, and with every listing one more agent is archived (newest: at the front).
        if number >= 2:
            agents = [place for place in agents if place[1] not in ("agent-01", "agent-15")]
        return [(2000 + n, f"new-{n}") for n in reversed(range(2, number + 1))] + agents

    builds = _listings(monkeypatch, archive)
    shared, ctx = snapshot.SharedSnapshot(), object()

    seen, totals, refreshes, cursor = [], [], 0, ""
    for _ in range(20):
        page = _next(shared, ctx, cursor, 4)
        seen += [row["agent_id"] for row in page["agents"]]
        totals.append(page["total"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        first = _next(shared, ctx, "", 4)                 # the other client
        refreshes += 1
        assert first["agents"][0]["build"] == refreshes + 1 and first["agents"][0]["agent_id"] == f"new-{refreshes + 1}"

    assert builds == list(range(1, refreshes + 2))        # the walk's first page and each refresh
    assert len(seen) == len(set(seen))                    # nothing twice
    # Every agent that was archived when the walk came to its place, once and in order: the
    # one restored behind the walk had been listed, the one restored ahead of it is not, and
    # neither are the ones archived after the walk began (they sort before its position).
    assert seen == [f"agent-{n:02d}" for n in range(20) if n != 15]
    # ``total`` is the held listing's: 20, then 18 that stayed and one more with every refresh.
    assert totals == [20] + [18 + n for n in range(1, len(totals))]


def test_a_page_after_the_listing_expired_builds_a_new_one_and_continues(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    builds = _listings(monkeypatch, _fixed(6))
    shared, ctx = snapshot.SharedSnapshot(), object()

    first = _next(shared, ctx, "", 2)
    clock[0] += snapshot.ARCHIVED_LISTING_TTL_S           # exactly the limit: still held
    second = _next(shared, ctx, first["next_cursor"], 2)
    assert [row["agent_id"] for row in second["agents"]] == ["agent-002", "agent-003"] and builds == [1]

    clock[0] += 1                                         # now past it
    third = _next(shared, ctx, second["next_cursor"], 2)
    assert [row["agent_id"] for row in third["agents"]] == ["agent-004", "agent-005"]   # from the agent the cursor names
    assert builds == [1, 2] and third["next_cursor"] is None


def test_an_expired_listing_is_released_by_the_next_request_of_any_kind(monkeypatch):
    """Round two, item 4: 3.3 MB of rows for 1,019 archived agents stayed in memory until the
    next listing build, which on a Host whose clients never open the archive is never."""
    clock = [1000.0]
    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(snapshot, "build_agent_snapshot", lambda ctx, include_archived=True: {})
    builds = _listings(monkeypatch, _fixed(6))
    ctx = object()

    for ask in (lambda shared: shared.body(ctx),
                lambda shared: shared.body(ctx, include_archived=False),
                lambda shared: _next(shared, ctx, "", 2)):
        shared = snapshot.SharedSnapshot()
        _next(shared, ctx, "", 2)
        held = shared._archived
        assert held is not None and len(held.rows) == 6
        clock[0] += snapshot.ARCHIVED_LISTING_TTL_S
        shared.body(ctx, include_archived=False)
        assert shared._archived is held                   # not before its time
        clock[0] += 0.5
        ask(shared)
        assert shared._archived is not held               # gone, or replaced by the build just asked for
    assert shared._archived is not None and builds == [1, 2, 3, 4]

    # A snapshot request releases it and holds nothing in its place.
    shared = snapshot.SharedSnapshot()
    _next(shared, ctx, "", 2)
    clock[0] += snapshot.ARCHIVED_LISTING_TTL_S + 0.5
    shared.body(ctx, include_archived=False)
    assert shared._archived is None

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


def test_the_pages_of_one_walk_are_cut_from_one_listing(monkeypatch):
    builds = []
    rows = [{"n": n} for n in range(7)]
    order = [(100 - n, f"agent-{n}") for n in range(7)]
    monkeypatch.setattr(snapshot, "build_archived_listing", lambda ctx: builds.append(ctx) or (rows, order))
    shared, ctx = snapshot.SharedSnapshot(), object()

    seen, cursor = [], ""
    for _ in range(10):
        position, token = snapshot._decode_cursor(cursor) if cursor else (None, "")
        page = shared.archived_page(ctx, limit=3, position=position, token=token)
        seen += [row["n"] for row in page["agents"]]
        assert page["total"] == 7
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == list(range(7))
    assert len(builds) == 1                               # three pages, one build

    # A new walk (no cursor) is a new listing: it must see what was written since.
    again = shared.archived_page(ctx, limit=3)
    assert len(builds) == 2
    # Another context never shares it, even with a cursor of this listing.
    position, token = snapshot._decode_cursor(again["next_cursor"])
    shared.archived_page(object(), limit=3, position=position, token=token)
    assert len(builds) == 3


def test_a_walk_slower_than_the_listing_is_held_continues_in_a_new_one(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    builds = []
    rows = [{"n": n} for n in range(6)]
    order = [(100 - n, f"agent-{n}") for n in range(6)]
    monkeypatch.setattr(snapshot, "build_archived_listing", lambda ctx: builds.append(ctx) or (rows, order))
    shared, ctx = snapshot.SharedSnapshot(), object()

    first = shared.archived_page(ctx, limit=2)
    position, token = snapshot._decode_cursor(first["next_cursor"])
    clock[0] += snapshot.ARCHIVED_LISTING_TTL_S - 1       # still held
    second = shared.archived_page(ctx, limit=2, position=position, token=token)
    assert [row["n"] for row in second["agents"]] == [2, 3] and len(builds) == 1

    position, token = snapshot._decode_cursor(second["next_cursor"])
    clock[0] += 2                                         # now past it
    third = shared.archived_page(ctx, limit=2, position=position, token=token)
    assert [row["n"] for row in third["agents"]] == [4, 5]    # from the agent the cursor names
    assert len(builds) == 2 and third["next_cursor"] is None

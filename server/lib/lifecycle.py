"""Lifecycle kit: one owner, compare-and-set transitions, deadlines, outbox.

A lifecycle is a row whose state column moves through a declared graph. The
contract is in docs/architecture/lifecycles.md; in short:

1. One owner. Only ``Lifecycle.owner`` writes ``state_column`` (guarded by
   tests/unit/test_table_writers.py).
2. Atomic transitions. ``transition`` reads the state, decides and writes
   ``UPDATE t SET state=? WHERE key=? AND state=?`` inside one transaction;
   zero rows is a ``Conflict``, never a silent overwrite.
3. Deadlines. Every non-final state declares a ``Deadline`` (the reaper moves
   it on, to a final state or back to an earlier one) or a ``WaitForever``
   with the reason. ``reap`` works in bounded batches; ``reap_tick`` keeps a
   host's tick within a row and time budget.
4. Decide-then-act. ``Lifecycle.decide`` is pure; side effects go into the
   ``Outbox`` of ``transaction`` and run only after COMMIT.

An owner declares its lifecycle as a module-level ``Lifecycle(...)``; the
guard tests find every declaration by scanning server/. The kit imports
nothing from the server so any layer can use it.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, Literal, Protocol, TypeVar

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
EXPIRED = "deadline_expired"  # the event the reaper records


class LifecycleError(Exception):
    """A lifecycle declaration or kit call is malformed."""


class IllegalTransition(LifecycleError):
    def __init__(self, lifecycle: str, state: str | None, event: str):
        super().__init__(f"{lifecycle}: event {event!r} is not legal from {state!r}")
        self.lifecycle, self.state, self.event = lifecycle, state, event


class Conflict(LifecycleError):
    """The compare-and-set matched zero rows: someone else moved the row first."""

    def __init__(self, lifecycle: str, key: object, expected: str | None):
        super().__init__(f"{lifecycle}: {key!r} is no longer in {expected!r}")
        self.lifecycle, self.key, self.expected = lifecycle, key, expected


@dataclass(frozen=True)
class Deadline:
    after_ms: int
    # Usually a final state. A non-final target (a stale claim back to queued)
    # is allowed when it has its own wait; deadlines alone may not cycle.
    expire_to: str


@dataclass(frozen=True)
class WaitForever:
    reason: str


@dataclass(frozen=True)
class Edge:
    event: str
    sources: frozenset[str]
    target: str
    # A correction rewrites one final state to another (a newer settlement
    # wins). Only final sources and a final target; the declaration lists
    # every allowed pair. Re-applying it to a row already in its target is a
    # no-op, not a Conflict.
    correction: bool = False


@dataclass(frozen=True)
class Transition:
    key: object
    event: str
    source: str
    target: str
    at: int


@dataclass(frozen=True)
class Lifecycle:
    name: str
    owner: str  # module path under server/, e.g. "lib/turn_lifecycle.py"
    initial: str
    final: frozenset[str]
    edges: tuple[Edge, ...]
    waits: Mapping[str, Deadline | WaitForever]
    # The SQL binding. Leave all four empty for an in-memory lifecycle, whose
    # owner applies decide() under its own lock.
    table: str = ""
    key: str = ""
    state_column: str = ""
    entered_column: str = ""  # epoch ms of the last transition; the reaper reads it
    # Instead of one entered_column: a column per state that records its own
    # entry time (claimed -> claimed_at). A transition stamps only its target's
    # column, if it has one; the reaper reads the source state's column.
    entered_columns: Mapping[str, str] = field(default_factory=dict)
    _table: dict[tuple[str, str], str] = field(init=False, repr=False, compare=False)
    _settled: frozenset[tuple[str, str]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise LifecycleError(f"{self.name}: " + "; ".join(problems))
        table = {(e.event, s): e.target for e in self.edges for s in e.sources}
        object.__setattr__(self, "_table", table)
        # (event, state): a correction re-applied to a row already in its target.
        object.__setattr__(self, "_settled", frozenset((e.event, e.target) for e in self.edges if e.correction))

    @property
    def states(self) -> frozenset[str]:
        found = {self.initial, *self.final}
        for edge in self.edges:
            found |= edge.sources | {edge.target}
        return frozenset(found)

    def problems(self) -> list[str]:
        binding = (self.table, self.key, self.state_column)
        stamps = ((self.entered_column,) if not self.entered_columns else ()) + tuple(self.entered_columns.values())
        out = [] if not any(binding + stamps) else [
            f"bad identifier {v!r}" for v in binding + stamps if not _IDENT.match(v)]
        if self.entered_column and self.entered_columns:
            out.append("use entered_column or entered_columns, not both")
        if self.entered_columns and not self.table:
            out.append("entered_columns needs a table")
        out += [f"entered_columns names unknown state {s!r}" for s in sorted(set(self.entered_columns) - self.states)]
        if self.entered_columns:
            out += [f"state {s!r} has a deadline but no entered column" for s, w in sorted(self.waits.items())
                    if isinstance(w, Deadline) and s not in self.entered_columns]
        if not self.final:
            out.append("no final state")
        if self.initial in self.final:
            out.append(f"initial state {self.initial!r} is final")
        corrections = {e.event for e in self.edges if e.correction}
        mixed = sorted(corrections & {e.event for e in self.edges if not e.correction})
        out += [f"event {event!r} is both a correction and an ordinary edge" for event in mixed]
        seen: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.event == EXPIRED:
                out.append(f"{EXPIRED!r} is reserved for the reaper")
            if edge.correction:
                outside = sorted((edge.sources | {edge.target}) - self.final)
                if outside:
                    out.append(f"correction {edge.event!r} must run between final states, not {outside}")
                if edge.target in edge.sources:
                    out.append(f"correction {edge.event!r} lists its own target {edge.target!r} as a source")
            for source in sorted(edge.sources):
                if source in self.final and not edge.correction:
                    out.append(f"edge {edge.event!r} leaves final state {source!r}")
                if (edge.event, source) in seen:
                    out.append(f"event {edge.event!r} has two targets from {source!r}")
                seen.add((edge.event, source))
        for state in sorted(self.states - self.final):
            wait = self.waits.get(state)
            if wait is None:
                out.append(f"non-final state {state!r} has no deadline or WaitForever")
            elif isinstance(wait, Deadline) and (wait.after_ms <= 0 or wait.expire_to not in self.states
                                                 or wait.expire_to == state):
                out.append(f"deadline of {state!r} must be positive and end in another declared state")
            elif not isinstance(wait, (Deadline, WaitForever)):
                out.append(f"wait of {state!r} must be a Deadline or a WaitForever, not {type(wait).__name__}")
            elif isinstance(wait, WaitForever) and not wait.reason.strip():
                out.append(f"WaitForever of {state!r} needs a reason")
        out += [f"final or unknown state {s!r} has a wait" for s in sorted(set(self.waits) - (self.states - self.final))]
        expiries = {(s, w.expire_to) for s, w in self.waits.items() if isinstance(w, Deadline)}
        expire = dict(expiries)
        for start in sorted(expire):
            seen, here = [start], expire[start]
            while here in expire and here not in seen:
                seen.append(here)
                here = expire[here]
            if here == start:
                out.append(f"deadlines alone cycle: {' -> '.join(seen + [start])}")
        forward = {(s, e.target) for e in self.edges for s in e.sources} | expiries
        reachable = _closure({self.initial}, forward)
        out += [f"state {s!r} is unreachable from {self.initial!r}" for s in sorted(self.states - reachable)]
        back = {(b, a) for a, b in forward}
        out += [f"state {s!r} cannot reach a final state" for s in sorted(self.states - _closure(set(self.final), back))]
        return out

    def decide(self, state: str | None, event: str) -> str:
        """Pure: the target of ``event`` from ``state``, or IllegalTransition."""
        return _decide(self.name, self._table, self.already_corrected, state, event)

    def already_corrected(self, state: str | None, event: str) -> bool:
        """``event`` is a correction whose target ``state`` already is: a no-op.
        Only corrections: an ordinary self-loop edge (a lease renewal) is a real
        transition and is written."""
        return (event, state) in self._settled and (event, state) not in self._table

    def entered_of(self, state: str) -> str:
        """The column that holds ``state``'s entry time ('' when it has none)."""
        return self.entered_columns.get(state, "") if self.entered_columns else self.entered_column

    def deadline(self, state: str, entered_at: int) -> int | None:
        wait = self.waits.get(state)
        return entered_at + wait.after_ms if isinstance(wait, Deadline) else None

    def overdue(self, state: str, entered_at: int, now: int) -> bool:
        due = self.deadline(state, entered_at)
        return due is not None and now >= due


def _decide(name, table, already_corrected, state, event):
    """Shared pure lookup; declaration validation remains front-end specific."""
    target = table.get((event, state)) if state is not None else None
    if target is None and already_corrected(state, event):
        return state
    if target is None:
        raise IllegalTransition(name, state, event)
    return target


def _closure(start: set[str], pairs: set[tuple[str, str]]) -> set[str]:
    seen, todo = set(start), list(start)
    while todo:
        here = todo.pop()
        for a, b in pairs:
            if a == here and b not in seen:
                seen.add(b)
                todo.append(b)
    return seen


class EffectsFailed(ExceptionGroup):
    """Effects that raised after the transaction committed; the state stands."""


@dataclass
class Outbox:
    effects: list[Callable[[], object]] = field(default_factory=list)

    def add(self, effect: Callable[[], object]) -> None:
        self.effects.append(effect)

    def run(self) -> None:
        failures = []
        for effect in self.effects:
            try:
                effect()
            except Exception as exc:  # every effect runs; all failures surface below
                failures.append(exc)
        self.effects.clear()
        if failures:
            raise EffectsFailed("lifecycle effects failed after commit", failures)


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[Outbox]:
    """BEGIN IMMEDIATE ... COMMIT; then run the outbox. Rollback drops it."""
    if con.in_transaction:
        raise LifecycleError("lifecycle transaction cannot nest in an open transaction")
    con.execute("BEGIN IMMEDIATE")
    outbox = Outbox()
    try:
        yield outbox
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:  # SQLite may already have rolled back (SQLITE_FULL, ...)
            con.execute("ROLLBACK")
        raise
    outbox.run()


def _columns(lc: Lifecycle, values: Mapping[str, object] | None, target: str) -> dict[str, object]:
    """The writes of a transition into ``target``: its state, its entry stamp
    (if the state has an entered column) and the caller's values, which may
    not touch the key, the state or that stamp."""
    values = dict(values or {})
    reserved = {lc.key, lc.state_column, lc.entered_of(target)} - {""}
    reserved &= set(values)
    bad = [c for c in values if not _IDENT.match(c)]
    if reserved or bad:
        raise LifecycleError(f"{lc.name}: cannot set {sorted(reserved) + bad}")
    return values


def _stamp(lc: Lifecycle, state: str, now: int) -> dict[str, object]:
    stamp = lc.entered_of(state)
    return {lc.state_column: state, **({stamp: now} if stamp else {})}


def _require_txn(con: sqlite3.Connection, lc: Lifecycle) -> None:
    if not lc.table:
        raise LifecycleError(f"{lc.name}: in-memory lifecycle has no table")
    if not con.in_transaction:
        raise LifecycleError(f"{lc.name}: call inside lifecycle.transaction()")


def create(con: sqlite3.Connection, lc: Lifecycle, key: object, *, now: int,
           values: Mapping[str, object] | None = None) -> Transition:
    _require_txn(con, lc)
    cols = {lc.key: key, **_stamp(lc, lc.initial, now), **_columns(lc, values, lc.initial)}
    con.execute(f"INSERT INTO {lc.table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()))
    return Transition(key, "created", "", lc.initial, now)


def compare_and_set(con: sqlite3.Connection, lc: Lifecycle, key: object, *, event: str,
                    source: str, target: str, now: int,
                    values: Mapping[str, object] | None = None) -> Transition:
    _require_txn(con, lc)
    cols = {**_stamp(lc, target, now), **_columns(lc, values, target)}
    assignments = ", ".join(f"{c} = ?" for c in cols)
    cursor = con.execute(
        f"UPDATE {lc.table} SET {assignments} WHERE {lc.key} = ? AND {lc.state_column} = ?",
        (*cols.values(), key, source))
    if cursor.rowcount != 1:
        raise Conflict(lc.name, key, source)
    return Transition(key, event, source, target, now)


def compare_and_set_where(con: sqlite3.Connection, lc: Lifecycle, events: str | Sequence[str], *,
                          where: str, params: Sequence[object] = (), now: int,
                          values: Mapping[str, object] | None = None,
                          returning: str = "") -> int | list:
    """A set-based compare-and-set, for an owner that moves every matching row
    in one statement: ``UPDATE t SET state = <target> ... WHERE state IN
    (<declared sources of the events>) AND (<where>)``, one statement per
    target. The sources come from the declaration, never from the caller, so
    a row in any other state (a final one in particular) is never touched; a
    correction never matches a row already in its target. ``where`` is the
    owner's own SQL (never user input). Returns the number of rows moved, or
    the ``returning`` columns of every moved row.

    Transaction rule: inside an open transaction the call joins it. Outside
    one, a single target runs as one autocommit statement (atomic by itself,
    as the owner's writers run today), and several targets are wrapped in one
    BEGIN IMMEDIATE ... COMMIT of their own."""
    if not lc.table:
        raise LifecycleError(f"{lc.name}: in-memory lifecycle has no table")
    events = (events,) if isinstance(events, str) else tuple(events)
    by_target: dict[str, set[str]] = {}
    for (event, source), target in lc._table.items():
        if event in events:
            by_target.setdefault(target, set()).add(source)
    unknown = [e for e in events if not any(k[0] == e for k in lc._table)]
    if unknown:
        raise LifecycleError(f"{lc.name}: no edge for event(s) {unknown}")
    # One statement per target is only the events' meaning when no row can
    # match two of them: the source sets must not overlap (one source, two
    # targets, would resolve to whichever ran first), and no target may be a
    # source of the call (a row moved by one statement would match the next).
    if len(by_target) > 1:
        sources = [src for _, src in sorted(by_target.items())]
        shared = sorted({s for i, a in enumerate(sources) for b in sources[i + 1:] for s in a & b})
        chained = sorted(set(by_target) & set().union(*sources))
        if shared or chained:
            raise LifecycleError(
                f"{lc.name}: events {list(events)} cannot run as one set-based write: "
                + (f"sources {shared} have two targets" if shared else "")
                + ("; " if shared and chained else "")
                + (f"targets {chained} are also sources" if chained else ""))
    if returning and returning != "*" and not all(_IDENT.match(c.strip()) for c in returning.split(",")):
        raise LifecycleError(f"{lc.name}: bad returning columns {returning!r}")
    own = len(by_target) > 1 and not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        moved = _cas_where(con, lc, by_target, where, params, now, values, returning)
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own and con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return moved


def _cas_where(con, lc, by_target, where, params, now, values, returning):
    moved: int | list = [] if returning else 0
    for target, sources in sorted(by_target.items()):
        cols = {**_stamp(lc, target, now), **_columns(lc, values, target)}
        ordered = sorted(sources)
        cursor = con.execute(
            f"UPDATE {lc.table} SET {', '.join(f'{c} = ?' for c in cols)}"
            f" WHERE {lc.state_column} IN ({', '.join('?' * len(ordered))}) AND ({where})"
            + (f" RETURNING {returning}" if returning else ""),
            (*cols.values(), *ordered, *params))
        if returning:
            moved += cursor.fetchall()
        else:
            moved += cursor.rowcount
    return moved


def transition(con: sqlite3.Connection, lc: Lifecycle, key: object, event: str, *, now: int,
               values: Mapping[str, object] | None = None) -> Transition:
    """Read, decide (pure) and compare-and-set, all in the caller's transaction."""
    _require_txn(con, lc)
    row = con.execute(f"SELECT {lc.state_column} FROM {lc.table} WHERE {lc.key} = ?", (key,)).fetchone()
    source = row[0] if row else None
    target = lc.decide(source, event)
    if lc.already_corrected(source, event):  # nothing to write
        return Transition(key, event, source, target, now)
    return compare_and_set(con, lc, key, event=event, source=source, target=target, now=now, values=values)


@dataclass(frozen=True)
class ReapBatch:
    moved: list[Transition]
    examined: int  # rows looked at, moved or skipped by ``eligible``
    cursor: tuple[str, int, object] | None  # resume after (state, entered_at, key); None: all done


def reap(con: sqlite3.Connection, lc: Lifecycle, *, now: int, limit: int = 100,
         after: tuple[str, int, object] | None = None,
         eligible: Callable[[dict], str | None] | None = None) -> ReapBatch:
    """One bounded batch, in the caller's transaction: at most ``limit`` overdue
    rows, in keyset order (state, entered_at, key) from ``after``.

    ``eligible(row)`` (the full row as a dict) decides per row, inside the
    transaction: None skips it (it still counts toward ``limit``, and the next
    pass from the start sees it again), ``EXPIRED`` moves it to its deadline's
    target, any other event goes through ``decide`` (for example ``abandon``
    once a persisted attempt count reaches its cap). It must be pure or read
    only SQLite: no process, runtime or network calls under the writer lock.
    Default: every overdue row expires."""
    _require_txn(con, lc)
    if limit <= 0:
        raise LifecycleError(f"{lc.name}: reap limit must be positive")
    deadlines = sorted((s, w) for s, w in lc.waits.items() if isinstance(w, Deadline))
    moved: list[Transition] = []
    examined = 0
    for state, wait in deadlines:
        if after is not None and state < after[0]:
            continue
        # Validation guarantees a column for a Deadline state. A NULL entry
        # time counts as 0 (overdue), as the queue's claim reset does today.
        entered = f"COALESCE({lc.entered_of(state)}, 0)"
        keyset, args = "", []
        if after is not None and state == after[0]:
            keyset, args = f" AND ({entered}, {lc.key}) > (?, ?)", [after[1], after[2]]
        cursor = con.execute(
            f"SELECT * FROM {lc.table} WHERE {lc.state_column} = ? AND {entered} <= ?{keyset}"
            f" ORDER BY {entered}, {lc.key} LIMIT ?",
            (state, now - wait.after_ms, *args, limit - examined))
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, r)) for r in cursor.fetchall()]
        for row in rows:
            examined += 1
            event = eligible(row) if eligible is not None else EXPIRED
            if event is not None:
                target = wait.expire_to if event == EXPIRED else lc.decide(state, event)
                moved.append(compare_and_set(con, lc, row[lc.key], event=event, source=state,
                                             target=target, now=now))
            if examined == limit:
                return ReapBatch(moved, examined, (state, row[lc.entered_of(state)] or 0, row[lc.key]))
    return ReapBatch(moved, examined, None)


def reap_tick(con: sqlite3.Connection, lc: Lifecycle, *, now: int,
              cursor: tuple[str, int, object] | None = None, batch: int = 100,
              max_rows: int = 500, max_ms: float = 200.0,
              eligible: Callable[[dict], str | None] | None = None,
              effects: Callable[[Outbox, list[Transition]], None] | None = None,
              clock: Callable[[], float] = time.perf_counter
              ) -> tuple[list[Transition], tuple[str, int, object] | None]:
    """A host's tick: ``reap`` batches, each in its own ``transaction`` (so the
    writer lock is released between them), until ``max_rows`` rows were
    examined, ``max_ms`` of time inside the batches was spent, or every
    overdue row was seen. ``effects(outbox, moved)`` queues each batch's
    after-commit effects. Returns everything moved and the cursor for the next
    tick (None: start from the beginning)."""
    moved: list[Transition] = []
    examined, spent = 0, 0.0
    while examined < max_rows and spent < max_ms:
        started = clock()
        with transaction(con) as outbox:
            result = reap(con, lc, now=now, limit=min(batch, max_rows - examined),
                          after=cursor, eligible=eligible)
            if effects is not None:
                effects(outbox, result.moved)
            spent += (clock() - started) * 1000.0  # inside the batch, before COMMIT and effects
        moved += result.moved
        examined += result.examined
        cursor = result.cursor
        if cursor is None:
            break
    return moved, cursor


# Additive, owner-executed API. No SQL/queue/reaper consumer is migrated here.
ExitKind = Literal["event", "deadline", "user", "refused", "error", "closed", "superseded"]
RefusalCode = str


class DecisionPolicy(Protocol):
    def decide(self, state: str | None, event: str) -> str: ...


@dataclass(frozen=True)
class WaitExit:
    kind: ExitKind
    name: str
    deadline_source: str = ""


@dataclass(frozen=True, kw_only=True)
class NamedWait:
    name: str
    exits: tuple[WaitExit, ...]
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "exits", tuple(self.exits))


def _names(values, label):
    names = frozenset(values)
    if any(not isinstance(v, str) or not v.strip() or len(v) > 128 for v in names):
        raise LifecycleError(f"{label}: expected nonempty bounded names")
    return names


@dataclass(frozen=True, kw_only=True)
class Rules:
    """Opt-in finite policy, independent of storage and owner synchronization."""
    family: str
    owner: str
    states: frozenset[str]
    events: frozenset[str]
    initial: str
    final: frozenset[str]
    edges: tuple[Edge, ...]
    waits: Mapping[str, NamedWait]
    refusal_codes: frozenset[str]
    transient: frozenset[str] = frozenset()
    cyclic: bool = False
    _table: Mapping = field(init=False, repr=False, compare=False)
    _settled: frozenset = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        _names((self.family, self.owner), "owner")
        for name in ("states", "events", "final", "refusal_codes", "transient"):
            object.__setattr__(self, name, _names(getattr(self, name), name))
        if any(not isinstance(edge, Edge) for edge in self.edges):
            raise LifecycleError("edges must be Edge declarations")
        object.__setattr__(self, "edges", tuple(
            Edge(e.event, frozenset(e.sources), e.target, e.correction) for e in self.edges))
        object.__setattr__(self, "waits", MappingProxyType(dict(self.waits)))
        self._validate_graph()
        table = {(e.event, source): e.target for e in self.edges for source in e.sources}
        object.__setattr__(self, "_table", MappingProxyType(table))
        object.__setattr__(self, "_settled", frozenset((e.event, e.target) for e in self.edges if e.correction))
        self._validate_waits()

    def _validate_graph(self):
        if self.initial not in self.states or self.initial in self.final:
            raise LifecycleError("initial must be a declared non-final state")
        if not self.final <= self.states or (not self.cyclic and not self.final):
            raise LifecycleError("finite policy needs declared final states")
        if not self.transient <= self.states - self.final:
            raise LifecycleError("transient must name non-final states")
        seen, corrections, ordinary = set(), set(), set()
        for edge in self.edges:
            if (edge.event not in self.events or not edge.sources
                    or not edge.sources <= self.states or edge.target not in self.states):
                raise LifecycleError("edge uses undeclared vocabulary or has no sources")
            if edge.correction:
                corrections.add(edge.event)
                if not (edge.sources | {edge.target}) <= self.final or edge.target in edge.sources:
                    raise LifecycleError("correction must move between distinct final states")
            else:
                ordinary.add(edge.event)
                if edge.sources & self.final:
                    raise LifecycleError("ordinary edge leaves a final state")
            for source in edge.sources:
                pair = (edge.event, source)
                if pair in seen:
                    raise LifecycleError("duplicate event/source pair")
                seen.add(pair)
        if corrections & ordinary:
            raise LifecycleError("event mixes correction and ordinary edges")
        forward = {(s, e.target) for e in self.edges for s in e.sources}
        if self.states - _closure({self.initial}, forward):
            raise LifecycleError("unreachable declared state")
        if not self.cyclic and self.states - _closure(set(self.final), {(b, a) for a, b in forward}):
            raise LifecycleError("state cannot reach a final state")

    def _validate_waits(self):
        allowed = self.states - self.final - self.transient
        if set(self.waits) != allowed:
            raise LifecycleError("each waiting state needs exactly one NamedWait")
        kinds = {"event", "deadline", "user", "refused", "error", "closed", "superseded"}
        for state, wait in self.waits.items():
            if not isinstance(wait, NamedWait) or not wait.name.strip() or not wait.exits:
                raise LifecycleError("wait needs a name and existing exits")
            seen = set()
            for exit in wait.exits:
                if not isinstance(exit, WaitExit) or exit.kind not in kinds or not exit.name.strip():
                    raise LifecycleError("invalid named wait exit")
                pair = (exit.kind, exit.name)
                if pair in seen:
                    raise LifecycleError("duplicate wait exit")
                seen.add(pair)
                if exit.kind == "deadline":
                    if not exit.deadline_source.strip():
                        raise LifecycleError("deadline exit needs its existing owner source")
                elif exit.deadline_source:
                    raise LifecycleError("only deadline exits name a deadline source")
                if exit.kind == "event" and (exit.name, state) not in self._table:
                    raise LifecycleError("wait event is not legal from its state")

    def decide(self, state: str | None, event: str) -> str:
        return _decide(self.family, self._table, self.already_corrected, state, event)

    def already_corrected(self, state: str | None, event: str) -> bool:
        return (event, state) in self._settled and (event, state) not in self._table


E = TypeVar("E")
P = TypeVar("P")
T = TypeVar("T")


@dataclass(frozen=True)
class OwnedEvent(Generic[E, P]):
    name: str
    entity_id: str
    expected: E
    payload: P


@dataclass(frozen=True)
class Applied(Generic[T]):
    value: T


@dataclass(frozen=True)
class NoOp(Generic[T]):
    value: T


@dataclass(frozen=True)
class Refused:
    code: RefusalCode
    observed_state: str | None


@dataclass(frozen=True)
class WriteConflict:
    code: RefusalCode


Outcome = Applied[T] | NoOp[T] | Refused | WriteConflict


@dataclass(frozen=True)
class SafeIds:
    entity_id: str | int | None = None
    trace_id: str | int | None = None
    operation_id: str | int | None = None
    generation: str | int | None = None

    def __post_init__(self):
        for name in ("entity_id", "trace_id", "operation_id", "generation"):
            value = getattr(self, name)
            if value is not None and type(value) not in (str, int):
                raise LifecycleError("diagnostic IDs must be scalar, never proof/payload objects")
            if isinstance(value, str):
                object.__setattr__(self, name, value[:128])
            elif isinstance(value, int) and value.bit_length() > 64:
                object.__setattr__(self, name, "<large>")


@dataclass(frozen=True)
class RefusalNotice:
    family: str
    event: str
    state: str
    reason: RefusalCode
    ids: SafeIds


@dataclass(frozen=True, kw_only=True)
class RefusalSink:
    """Bounded memory only on capture; the OWNER supplies existing synchronization.

    Snapshot/reset use the same owner boundary. The kit creates no mutex or
    logging/SQL dependency. report runs outside that boundary via apply_owned.
    """
    family: str
    events: frozenset[str]
    states: frozenset[str]
    refusal_codes: frozenset[str]
    report: Callable[[RefusalNotice], None] = field(repr=False, compare=False)
    recent_limit: int = 256
    _counts: Counter = field(init=False, repr=False, compare=False)
    _recent: deque = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        _names((self.family,), "family")
        for name in ("events", "states", "refusal_codes"):
            object.__setattr__(self, name, _names(getattr(self, name), name))
        if not callable(self.report) or not 1 <= self.recent_limit <= 256:
            raise LifecycleError("sink needs a reporter and a recent limit in 1..256")
        object.__setattr__(self, "_counts", Counter())
        object.__setattr__(self, "_recent", deque(maxlen=self.recent_limit))

    def capture(self, event: str, state: str | None, reason: RefusalCode, ids: SafeIds) -> RefusalNotice:
        if reason not in self.refusal_codes or not isinstance(ids, SafeIds):
            raise LifecycleError("refusal uses undeclared code or unsafe IDs")
        notice = RefusalNotice(self.family, event if event in self.events else "<unknown>",
                               state if state in self.states else "<unknown>", reason, ids)
        self._counts[(notice.event, notice.state, reason)] += 1
        self._recent.append(notice)
        return notice

    def emit(self, notice: RefusalNotice) -> None:
        self.report(notice)

    @property
    def counts(self) -> Mapping:
        return MappingProxyType(dict(self._counts))

    @property
    def recent(self) -> tuple[RefusalNotice, ...]:
        return tuple(self._recent)

    def reset(self) -> None:
        self._counts.clear()
        self._recent.clear()


class RejectAtWrite(Protocol):
    def __call__(self, reason: RefusalCode, state: str | None, ids: SafeIds,
                 *, primary: BaseException | None = None) -> Refused: ...


class OwnerWritePort(Generic[E, P, T]):
    """One callsite-owned facade, never shared/reused. No domain state cache.

    A concrete owner MUST unwind its existing protected spans before returning
    or raising from write. This base cannot certify an arbitrary owner atomic.
    """
    _attempt_used = False

    def _claim_attempt(self):
        if self._attempt_used:
            raise LifecycleError("owner attempt facade is single-use")
        self._attempt_used = True

    def write(self, policy: DecisionPolicy, event: OwnedEvent[E, P], reject: RejectAtWrite) -> Outcome[T]:
        raise NotImplementedError("owner must implement its whole synchronized write")

    def after_boundary(self, report: Callable[[], None]) -> None:
        report()


def _primary_with_cause(primary, traceback, secondary):
    if secondary is primary:
        raise primary.with_traceback(traceback)
    raise primary.with_traceback(traceback) from secondary


def apply_owned(policy: DecisionPolicy, event: OwnedEvent[E, P],
                owner: OwnerWritePort[E, P, T], refusals: RefusalSink) -> Outcome[T]:
    """One owner write, then synchronous diagnostics after its actual unwind.

    No S42 hooks, owner finalizer, SQL joining, deferred registry or retries.
    """
    owner._claim_attempt()
    notice = None
    rejection = None
    emission_started = False

    def reject_once(reason, state, ids, *, primary=None):
        nonlocal notice, rejection
        if rejection is None:
            try:
                notice = refusals.capture(event.name, state, reason, ids)
            except BaseException as capture_error:
                if primary is not None:
                    _primary_with_cause(primary, primary.__traceback__, capture_error)
                raise
            rejection = Refused(reason, state)
        return rejection

    def emit_once():
        nonlocal emission_started
        if emission_started:
            raise LifecycleError("diagnostic completion invoked twice")
        emission_started = True
        refusals.emit(notice)

    def complete():
        if notice is not None:
            owner.after_boundary(emit_once)
            if not emission_started:
                raise LifecycleError("diagnostic completion omitted")

    try:
        outcome = owner.write(policy, event, reject_once)
        if not isinstance(outcome, (Applied, NoOp, Refused, WriteConflict)):
            raise LifecycleError("owner must return an explicit lifecycle outcome")
    except BaseException as primary:
        traceback = primary.__traceback__
        try:
            complete()
        except BaseException as diagnostic_error:
            _primary_with_cause(primary, traceback, diagnostic_error)
        raise
    complete()
    return outcome

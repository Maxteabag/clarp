"""Lifecycle kit: one owner, compare-and-set transitions, deadlines, outbox.

A lifecycle is a row whose state column moves through a declared graph. The
contract is in docs/architecture/lifecycles.md; in short:

1. One owner. Only ``Lifecycle.owner`` writes ``state_column`` (guarded by
   tests/unit/test_table_writers.py).
2. Atomic transitions. ``transition`` reads the state, decides and writes
   ``UPDATE t SET state=? WHERE key=? AND state=?`` inside one transaction;
   zero rows is a ``Conflict``, never a silent overwrite.
3. Deadlines. Every non-final state declares a ``Deadline`` (the reaper moves
   it to a final state) or a ``WaitForever`` with the reason.
4. Decide-then-act. ``Lifecycle.decide`` is pure; side effects go into the
   ``Outbox`` of ``transaction`` and run only after COMMIT.

An owner declares its lifecycle as a module-level ``Lifecycle(...)``; the
guard tests find every declaration by scanning server/. The kit imports
nothing from the server so any layer can use it.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

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
    expire_to: str  # a final state


@dataclass(frozen=True)
class WaitForever:
    reason: str


@dataclass(frozen=True)
class Edge:
    event: str
    sources: frozenset[str]
    target: str


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
    _table: dict[tuple[str, str], str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise LifecycleError(f"{self.name}: " + "; ".join(problems))
        table = {(e.event, s): e.target for e in self.edges for s in e.sources}
        object.__setattr__(self, "_table", table)

    @property
    def states(self) -> frozenset[str]:
        found = {self.initial, *self.final}
        for edge in self.edges:
            found |= edge.sources | {edge.target}
        return frozenset(found)

    def problems(self) -> list[str]:
        binding = (self.table, self.key, self.state_column, self.entered_column)
        out = [] if not any(binding) else [f"bad identifier {v!r}" for v in binding if not _IDENT.match(v)]
        if not self.final:
            out.append("no final state")
        if self.initial in self.final:
            out.append(f"initial state {self.initial!r} is final")
        seen: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.event == EXPIRED:
                out.append(f"{EXPIRED!r} is reserved for the reaper")
            for source in sorted(edge.sources):
                if source in self.final:
                    out.append(f"edge {edge.event!r} leaves final state {source!r}")
                if (edge.event, source) in seen:
                    out.append(f"event {edge.event!r} has two targets from {source!r}")
                seen.add((edge.event, source))
        for state in sorted(self.states - self.final):
            wait = self.waits.get(state)
            if wait is None:
                out.append(f"non-final state {state!r} has no deadline or WaitForever")
            elif isinstance(wait, Deadline) and (wait.after_ms <= 0 or wait.expire_to not in self.final):
                out.append(f"deadline of {state!r} must be positive and end in a final state")
            elif isinstance(wait, WaitForever) and not wait.reason.strip():
                out.append(f"WaitForever of {state!r} needs a reason")
        out += [f"final or unknown state {s!r} has a wait" for s in sorted(set(self.waits) - (self.states - self.final))]
        reachable = _closure({self.initial}, {(s, e.target) for e in self.edges for s in e.sources})
        out += [f"state {s!r} is unreachable from {self.initial!r}" for s in sorted(self.states - reachable)]
        back = {(e.target, s) for e in self.edges for s in e.sources}
        back |= {(w.expire_to, s) for s, w in self.waits.items() if isinstance(w, Deadline)}
        out += [f"state {s!r} cannot reach a final state" for s in sorted(self.states - _closure(set(self.final), back))]
        return out

    def decide(self, state: str | None, event: str) -> str:
        """Pure: the target of ``event`` from ``state``, or IllegalTransition."""
        target = self._table.get((event, state)) if state is not None else None
        if target is None:
            raise IllegalTransition(self.name, state, event)
        return target

    def deadline(self, state: str, entered_at: int) -> int | None:
        wait = self.waits.get(state)
        return entered_at + wait.after_ms if isinstance(wait, Deadline) else None

    def overdue(self, state: str, entered_at: int, now: int) -> bool:
        due = self.deadline(state, entered_at)
        return due is not None and now >= due


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


def _columns(lc: Lifecycle, values: Mapping[str, object] | None) -> dict[str, object]:
    values = dict(values or {})
    reserved = {lc.key, lc.state_column, lc.entered_column} & set(values)
    bad = [c for c in values if not _IDENT.match(c)]
    if reserved or bad:
        raise LifecycleError(f"{lc.name}: cannot set {sorted(reserved) + bad}")
    return values


def _require_txn(con: sqlite3.Connection, lc: Lifecycle) -> None:
    if not lc.table:
        raise LifecycleError(f"{lc.name}: in-memory lifecycle has no table")
    if not con.in_transaction:
        raise LifecycleError(f"{lc.name}: call inside lifecycle.transaction()")


def create(con: sqlite3.Connection, lc: Lifecycle, key: object, *, now: int,
           values: Mapping[str, object] | None = None) -> Transition:
    _require_txn(con, lc)
    cols = {lc.key: key, lc.state_column: lc.initial, lc.entered_column: now, **_columns(lc, values)}
    con.execute(f"INSERT INTO {lc.table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()))
    return Transition(key, "created", "", lc.initial, now)


def compare_and_set(con: sqlite3.Connection, lc: Lifecycle, key: object, *, event: str,
                    source: str, target: str, now: int,
                    values: Mapping[str, object] | None = None) -> Transition:
    _require_txn(con, lc)
    cols = {lc.state_column: target, lc.entered_column: now, **_columns(lc, values)}
    assignments = ", ".join(f"{c} = ?" for c in cols)
    cursor = con.execute(
        f"UPDATE {lc.table} SET {assignments} WHERE {lc.key} = ? AND {lc.state_column} = ?",
        (*cols.values(), key, source))
    if cursor.rowcount != 1:
        raise Conflict(lc.name, key, source)
    return Transition(key, event, source, target, now)


def transition(con: sqlite3.Connection, lc: Lifecycle, key: object, event: str, *, now: int,
               values: Mapping[str, object] | None = None) -> Transition:
    """Read, decide (pure) and compare-and-set, all in the caller's transaction."""
    _require_txn(con, lc)
    row = con.execute(f"SELECT {lc.state_column} FROM {lc.table} WHERE {lc.key} = ?", (key,)).fetchone()
    source = row[0] if row else None
    target = lc.decide(source, event)
    return compare_and_set(con, lc, key, event=event, source=source, target=target, now=now, values=values)


def reap(con: sqlite3.Connection, lc: Lifecycle, *, now: int) -> list[Transition]:
    """Move every row past its state's deadline to that deadline's final state."""
    _require_txn(con, lc)
    moved = []
    for state, wait in sorted(lc.waits.items()):
        if not isinstance(wait, Deadline):
            continue
        rows = con.execute(
            f"SELECT {lc.key} FROM {lc.table} WHERE {lc.state_column} = ? AND {lc.entered_column} <= ?"
            f" ORDER BY {lc.entered_column}, {lc.key}", (state, now - wait.after_ms)).fetchall()
        for (key,) in rows:
            moved.append(compare_and_set(con, lc, key, event=EXPIRED, source=state,
                                         target=wait.expire_to, now=now))
    return moved

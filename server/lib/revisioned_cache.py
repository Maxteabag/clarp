"""Bounded, locked caches keyed by a cross-process revision.

A `RevisionedCache` remembers one value per key together with the revision it
was computed under. `get_or_compute` answers from memory only while the caller
presents the same revision; anything else recomputes and replaces the entry.
The revision is whatever proves the inputs unchanged: `db.change_stamp()` for
whole-table aggregates, a per-row revision tuple for per-agent projections.

Every cache registers itself by name so `reset_all()` (called autouse from
`tests/conftest.py`) empties them between tests, and so the module-state
guard has one place to look instead of a dict per module. Entries are
bounded: past `max_entries` the least recently used key is dropped, and with
a `weight` function also past `max_weight` total weight (bytes held or
hashed, say).
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, "RevisionedCache"] = {}


class RevisionedCache(Generic[K, V]):
    def __init__(self, name: str, max_entries: int = 256,
                 key_fn: Callable[[object], K] | None = None, *,
                 max_weight: int | None = None,
                 weight: Callable[[Hashable, V], int] | None = None) -> None:
        if not name:
            raise ValueError("a RevisionedCache needs a name")
        self.name = name
        self.max_entries = max(1, int(max_entries))
        self.max_weight = max_weight
        self._weight_fn = weight
        self._key_fn = key_fn
        self._lock = threading.Lock()
        self._entries: OrderedDict[K, tuple[Hashable, V]] = OrderedDict()
        self._weights: dict[K, int] = {}
        self.weight = 0
        self.hits = 0
        self.misses = 0
        with _REGISTRY_LOCK:
            if name in _REGISTRY:
                raise ValueError(f"RevisionedCache {name!r} is already registered")
            _REGISTRY[name] = self

    def _key(self, key: object) -> K:
        return self._key_fn(key) if self._key_fn is not None else key  # type: ignore[return-value]

    def get_or_compute(self, key: object, revision: Hashable,
                       compute: Callable[[], V]) -> V:
        """Return the value cached for `key` at `revision`, computing otherwise.

        `compute` runs outside the lock so a slow aggregate never blocks other
        keys; two threads racing on one key both compute and the later result
        wins, which is harmless because both saw the same revision.
        """
        k = self._key(key)
        with self._lock:
            entry = self._entries.get(k)
            if entry is not None and entry[0] == revision:
                self._entries.move_to_end(k)
                self.hits += 1
                return entry[1]
            self.misses += 1
        value = compute()
        with self._lock:
            self._store(k, revision, value)
        return value

    def get(self, key: object, revision: Hashable) -> tuple[bool, V | None]:
        """(True, value) when `key` is cached at exactly `revision`, else
        (False, None); a hit counts as recent use."""
        k = self._key(key)
        with self._lock:
            entry = self._entries.get(k)
            if entry is not None and entry[0] == revision:
                self._entries.move_to_end(k)
                self.hits += 1
                return True, entry[1]
            self.misses += 1
            return False, None

    def put(self, key: object, revision: Hashable, value: V) -> None:
        with self._lock:
            self._store(self._key(key), revision, value)

    def _store(self, k: K, revision: Hashable, value: V) -> None:
        """Insert or replace under the lock, then evict to both bounds."""
        self.weight -= self._weights.pop(k, 0)
        self._entries[k] = (revision, value)
        self._entries.move_to_end(k)
        if self._weight_fn is not None:
            self._weights[k] = int(self._weight_fn(revision, value))
            self.weight += self._weights[k]
        while len(self._entries) > self.max_entries or (
                self.max_weight is not None and self.weight > self.max_weight
                and len(self._entries) > 1):
            oldest, _ = self._entries.popitem(last=False)
            self.weight -= self._weights.pop(oldest, 0)
        if self.max_weight is not None and self.weight > self.max_weight:
            # A single entry heavier than the whole bound is not kept.
            self._entries.clear()
            self._weights.clear()
            self.weight = 0

    def peek(self, key: object) -> tuple[Hashable, V] | None:
        """The stored (revision, value) for `key` without touching recency."""
        with self._lock:
            return self._entries.get(self._key(key))

    def pop(self, key: object) -> None:
        with self._lock:
            k = self._key(key)
            self._entries.pop(k, None)
            self.weight -= self._weights.pop(k, 0)

    def retain(self, keys: Iterable[object]) -> None:
        """Drop every entry whose key is not in `keys`."""
        wanted = {self._key(k) for k in keys}
        with self._lock:
            for stale in [k for k in self._entries if k not in wanted]:
                del self._entries[stale]
                self.weight -= self._weights.pop(stale, 0)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._weights.clear()
            self.weight = 0
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def registered() -> dict[str, RevisionedCache]:
    with _REGISTRY_LOCK:
        return dict(_REGISTRY)


def reset_all() -> None:
    """Empty every registered cache. Autouse in tests; safe in production."""
    for cache in registered().values():
        cache.reset()

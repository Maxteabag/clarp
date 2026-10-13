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
a `weight(key, revision, value)` function also past `max_weight` total weight
(the bytes an entry holds, say). An entry heavier than the whole bound is not
kept at all.
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
                 weight: Callable[[Hashable, Hashable, V], int] | None = None) -> None:
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
        # Keys an open CacheScope protects, with how many scopes protect each.
        self._pins: dict[K, int] = {}
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

    def get(self, key: object, revision: Hashable, *,
            scope: "CacheScope | None" = None) -> tuple[bool, V | None]:
        """(True, value) when `key` is cached at exactly `revision`, else
        (False, None); a hit counts as recent use, and with a `scope` it is
        protected from that scope's later inserts."""
        k = self._key(key)
        with self._lock:
            entry = self._entries.get(k)
            if entry is not None and entry[0] == revision:
                self._entries.move_to_end(k)
                self.hits += 1
                if scope is not None:
                    self._pin(scope, k)
                return True, entry[1]
            self.misses += 1
            return False, None

    def put(self, key: object, revision: Hashable, value: V, *,
            scope: "CacheScope | None" = None) -> bool:
        """Cache `value`. Without a `scope` this is plain LRU (unchanged). With
        one, admission never evicts an entry an open scope protects: if the
        entry cannot fit otherwise it is not cached (returns False) and the
        cache is left as it was."""
        with self._lock:
            k = self._key(key)
            if scope is None:
                self._store(k, revision, value)
                return True
            return self._admit(scope, k, revision, value)

    # --- scan-resistant admission (opt-in) ---------------------------------
    #
    # One request that walks more entries than the cache holds would, under
    # plain LRU, evict the entries it needs again next time: every walk
    # misses everything. A scope protects what it has hit or inserted, so its
    # own later inserts cannot evict them; past the bound they are skipped.
    # The resident set stays stable and only the excess misses. Protection
    # lasts until the scope is released, and covers at most max_entries keys
    # per scope. Concurrent scopes that together protect more than fits make
    # later inserts skip until they are released; nothing is pinned forever.

    def scope(self) -> "CacheScope":
        return CacheScope(self)

    def _pin(self, scope: "CacheScope", k: K) -> None:
        if k in scope.keys or len(scope.keys) >= self.max_entries:
            return
        scope.keys.add(k)
        self._pins[k] = self._pins.get(k, 0) + 1

    def _release(self, scope: "CacheScope") -> None:
        with self._lock:
            for k in scope.keys:
                left = self._pins.get(k, 0) - 1
                if left > 0:
                    self._pins[k] = left
                else:
                    self._pins.pop(k, None)
            scope.keys.clear()

    def _admit(self, scope: "CacheScope", k: K, revision: Hashable, value: V) -> bool:
        size = int(self._weight_fn(k, revision, value)) if self._weight_fn is not None else 0
        if self.max_weight is not None and size > self.max_weight:
            return False
        # The key's own older entry (another revision) is replaced, not kept.
        weight = self.weight - self._weights.get(k, 0) + size
        count = len(self._entries) - (1 if k in self._entries else 0) + 1
        victims = []
        for other in self._entries:                       # oldest first
            if count <= self.max_entries and (
                    self.max_weight is None or weight <= self.max_weight):
                break
            if other == k or self._pins.get(other):
                continue
            victims.append(other)
            weight -= self._weights.get(other, 0)
            count -= 1
        if count > self.max_entries or (
                self.max_weight is not None and weight > self.max_weight):
            return False                                   # only protected entries left
        for other in victims:
            del self._entries[other]
            self.weight -= self._weights.pop(other, 0)
        self.weight -= self._weights.pop(k, 0)
        self._entries.pop(k, None)
        self._entries[k] = (revision, value)
        if self._weight_fn is not None:
            self._weights[k] = size
            self.weight += size
        self._pin(scope, k)
        return True

    def _store(self, k: K, revision: Hashable, value: V) -> None:
        """Insert or replace under the lock, then evict to both bounds."""
        self.weight -= self._weights.pop(k, 0)
        self._entries.pop(k, None)
        size = int(self._weight_fn(k, revision, value)) if self._weight_fn is not None else 0
        if self.max_weight is not None and size > self.max_weight:
            # Heavier than the whole bound: not kept, and the key's older
            # entry is gone too (it was for another revision). The rest stay.
            return
        self._entries[k] = (revision, value)
        if self._weight_fn is not None:
            self._weights[k] = size
            self.weight += size
        while len(self._entries) > self.max_entries or (
                self.max_weight is not None and self.weight > self.max_weight):
            oldest, _ = self._entries.popitem(last=False)
            self.weight -= self._weights.pop(oldest, 0)

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
            self._pins.clear()
            self.weight = 0
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class CacheScope:
    """One request's protection on a RevisionedCache; release it when the
    request ends (artifacts.summary_request does, in a finally)."""

    def __init__(self, cache: RevisionedCache) -> None:
        self.cache = cache
        self.keys: set = set()

    def release(self) -> None:
        self.cache._release(self)


def registered() -> dict[str, RevisionedCache]:
    with _REGISTRY_LOCK:
        return dict(_REGISTRY)


def reset_all() -> None:
    """Empty every registered cache. Autouse in tests; safe in production."""
    for cache in registered().values():
        cache.reset()

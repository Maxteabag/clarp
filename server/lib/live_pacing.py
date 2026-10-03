"""Pacing for live text and wake-ups.

A streaming reply used to rewrite its stored row on every token (Claude) or on
the leading edge of a 250 ms window with no trailing write (the other CLIs),
and the ``transcript-updated`` wake-up dropped the last change of a burst.
Here:

* ``stable_prefix`` is the part of a growing markdown text that will not be
  re-flowed: everything up to the last blank line, closed fence or list-item
  start, never inside an open fence (T3 Code's "paragraph" filter).
* ``LivePacer`` writes that prefix at most every ``interval``, releases a long
  paragraph line by line (or word by word) once it has been held for
  ``hold``, always ends a burst with a trailing write, and writes the final
  text at once.
* ``TrailingThrottle`` passes the first event of a burst at once and the last
  one when the window closes.

Speech never goes through here: voice is cut from the unpaced stream.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s")

Schedule = Callable[[float, Callable[[], None]], Any]


def _thread_timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(max(0.0, delay), fn)
    timer.daemon = True
    timer.start()
    return timer


def stable_prefix(text: str) -> str:
    """The longest prefix of ``text`` that ends at a markdown-stable boundary."""
    boundary = 0
    in_fence = False
    pos = 0
    for line in text.splitlines(keepends=True):
        end = pos + len(line)
        complete = line.endswith("\n")
        if _FENCE.match(line):
            if in_fence:
                in_fence = False
                if complete:
                    boundary = end
            else:
                in_fence = True
        elif not in_fence:
            if not line.strip() and complete:
                boundary = end
            elif pos and _LIST_ITEM.match(line):
                boundary = max(boundary, pos)
        pos = end
    return text[:boundary]


def soft_prefix(text: str) -> str:
    """Up to the last line break, else the last word break."""
    cut = text.rfind("\n")
    if cut < 0:
        cut = max(text.rfind(" "), text.rfind("\t"))
    return text[:cut + 1] if cut >= 0 else ""


class LivePacer:
    """Paced writes of one growing text. Thread-safe; ``write`` runs under the
    pacer's lock so writes never reorder."""

    def __init__(self, write: Callable[[str], Any], *, interval: float = 0.25,
                 hold: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 schedule: Schedule | None = None):
        self._write = write
        self._interval = interval
        self._hold = hold
        self._clock = clock
        self._schedule = schedule or _thread_timer
        self._lock = threading.RLock()
        self._pending = ""
        self._written = ""
        self._last_write: float | None = None
        self._held_since: float | None = None
        self._timer: Any = None
        self._timer_at: float | None = None

    @property
    def written(self) -> str:
        return self._written

    def offer(self, text: str, *, final: bool = False) -> None:
        with self._lock:
            self._pending = text
            if final:
                self._cancel()
                if text and text != self._written:
                    self._emit(text, self._clock())
                return
            if self._held_since is None:
                self._held_since = self._clock()
            self._evaluate()

    def flush(self) -> None:
        """Write whatever is pending now (end of a turn or segment)."""
        self.offer(self._pending, final=True)

    def cancel(self) -> None:
        with self._lock:
            self._cancel()

    def _release(self, now: float) -> str:
        release = stable_prefix(self._pending)
        if self._held_since is not None and now - self._held_since >= self._hold:
            soft = soft_prefix(self._pending)
            if len(soft) > len(release):
                release = soft
        return release

    def _evaluate(self) -> None:
        now = self._clock()
        release = self._release(now)
        fresh = bool(release) and release != self._written \
            and not self._written.startswith(release)
        if fresh:
            if self._last_write is None or now - self._last_write >= self._interval:
                self._emit(release, now)
                fresh = False
        if self._pending == self._written:
            self._cancel()
            return
        if fresh:
            target = self._last_write + self._interval
        else:
            target = (self._held_since or now) + self._hold
        self._arm(target, now)

    def _emit(self, text: str, now: float) -> None:
        self._write(text)
        self._written = text
        self._last_write = now
        self._held_since = now

    def _arm(self, target: float, now: float) -> None:
        if self._timer is not None and self._timer_at is not None and self._timer_at <= target:
            return
        self._cancel()
        self._timer_at = target
        self._timer = self._schedule(max(0.0, target - now), self._fire)

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            self._timer_at = None
            self._evaluate()

    def _cancel(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._timer = None
        self._timer_at = None


class TrailingThrottle:
    """At most one item per key per ``interval``: the first at once, the
    latest of the rest when the window closes."""

    def __init__(self, interval: float, *, clock: Callable[[], float] = time.monotonic,
                 schedule: Schedule | None = None):
        self._interval = interval
        self._clock = clock
        self._schedule = schedule or _thread_timer
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._pending: dict[str, tuple[Any, Callable[[Any], Any]]] = {}
        self._armed: set[str] = set()

    def submit(self, key: str, item: Any, emit: Callable[[Any], Any]) -> None:
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            if key not in self._armed and (last is None or now - last >= self._interval):
                self._last[key] = now
            else:
                self._pending[key] = (item, emit)
                if key not in self._armed:
                    self._armed.add(key)
                    delay = self._interval - (now - (last or now))
                    self._schedule(delay, lambda: self._fire(key))
                return
        emit(item)

    def _fire(self, key: str) -> None:
        with self._lock:
            self._armed.discard(key)
            pending = self._pending.pop(key, None)
            if pending is None:
                return
            self._last[key] = self._clock()
        item, emit = pending
        emit(item)

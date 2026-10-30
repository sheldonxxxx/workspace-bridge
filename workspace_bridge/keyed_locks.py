"""Reentrant locks created on demand per key and dropped when unused.

The Bridge serializes work per run, per route and per workspace rather than
globally, so one slow Node or adapter only delays work on the same key. The
database lock (``Service.lock``) is always acquired after, never before, a
keyed lock and is never held across Node or adapter I/O.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Hashable, Iterator


class LockOrderError(RuntimeError):
    """A keyed lock was requested while holding its inner lock."""


class KeyedLocks:
    def __init__(self, inner=None) -> None:
        # ``inner`` is always taken inside these locks, never held while
        # waiting for one: breaking that order fails at once, not as a deadlock.
        self._inner = inner
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # key -> [RLock, holders]

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        if self._inner is not None and self._inner._is_owned():
            raise LockOrderError("keyed lock requested while holding the database lock")
        with self._guard:
            entry = self._locks.setdefault(key, [threading.RLock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)

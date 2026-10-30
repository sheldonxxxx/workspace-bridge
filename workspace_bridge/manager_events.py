"""Bounded, process-local invalidation hints for the authenticated Manager.

Durable REST projections remain authoritative. Every connection starts with a
reset, so neither reconnects nor slow clients depend on replaying event bodies.
This transport is independent of the MCP webhook journal and contains only
run IDs: `run.changed` names a run, `state.changed` says some audited Bridge
state moved and the Manager should reread its snapshot.
"""
from __future__ import annotations

import asyncio
from collections import deque
import re
import threading
from dataclasses import dataclass, field


RUN_ID = re.compile(r"run_[0-9a-f]{24}\Z")


@dataclass(eq=False)
class RunEventSubscription:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    active: bool = True
    _pending: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _scheduled: bool = False

    def enqueue(self, event: tuple[str, str | None]) -> None:
        # Bound pending work before scheduling it onto the browser's loop.
        # A stalled loop must not accumulate one callback per runtime event.
        with self._lock:
            if not self.active:
                return
            if event[0] == "closed":
                self._pending.clear()
                self._pending.append(event)
            elif event in self._pending and event[0] == "state.changed":
                return
            elif len(self._pending) >= 64:
                self._pending.clear()
                self._pending.append(("reset", None))
            else:
                self._pending.append(event)
            if self._scheduled:
                return
            self._scheduled = True
        self.loop.call_soon_threadsafe(self._flush)

    def _flush(self) -> None:
        with self._lock:
            pending = tuple(self._pending)
            self._pending.clear()
            self._scheduled = False
        for event in pending:
            self.deliver(event)

    def deliver(self, event: tuple[str, str | None]) -> None:
        if not self.active:
            return
        if event[0] == "closed":
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(event)
        elif self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
            # A reset coalesces lost hints into a complete REST refresh.
            self.queue.put_nowait(("reset", None))
        else:
            self.queue.put_nowait(event)


class RunEventHub:
    MAX_SUBSCRIBERS = 32

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[RunEventSubscription] = set()
        self._closed = False

    def subscribe(self) -> RunEventSubscription:
        subscriber = RunEventSubscription(asyncio.get_running_loop())
        with self._lock:
            if self._closed or len(self._subscribers) >= self.MAX_SUBSCRIBERS:
                raise RuntimeError("Run event stream unavailable")
            self._subscribers.add(subscriber)
        subscriber.queue.put_nowait(("reset", None))
        return subscriber

    def unsubscribe(self, subscriber: RunEventSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
        subscriber.active = False

    def publish(self, run_id: str) -> None:
        # The publisher cannot place arbitrary runtime content on the stream.
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            return
        self._send(("run.changed", run_id))

    def publish_state(self) -> None:
        self._send(("state.changed", None))

    def _send(self, event: tuple[str, str | None]) -> None:
        with self._lock:
            if self._closed:
                return
            targets = tuple(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.enqueue(event)
            except RuntimeError:
                self.unsubscribe(subscriber)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            targets = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in targets:
            try:
                subscriber.enqueue(("closed", None))
            except RuntimeError:
                subscriber.active = False

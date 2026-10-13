"""Bounded Runtime Protocol event hints; snapshots remain authoritative."""
from __future__ import annotations

import re
import threading
import time

from .runtime import RuntimeUnavailable, RuntimeUnsupported

_TYPE = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_MAX_CURSOR = 9007199254740991


def _cursor(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= _MAX_CURSOR)


def _identity(value: object, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and (bool(value) or empty)
            and len(value) <= 200 and all(ord(char) >= 32 for char in value))


def validate_event_page(value: object, after: int) -> dict:
    """Validate ordering before consuming any untrusted adapter hint.

    The envelope cursor is the global head, while a page may contain only
    its first 100 events. It is never the cursor for a partially read page.
    Unknown event payload fields are discarded at this boundary.
    """
    if (not isinstance(value, dict) or not _identity(value.get("instanceId"))
            or not _cursor(value.get("cursor")) or value["cursor"] < after
            or not isinstance(value.get("events"), list)
            or len(value["events"]) > 100):
        raise RuntimeUnavailable("Runtime event page is invalid")
    previous = after
    events = []
    gap = False
    for item in value["events"]:
        if (not isinstance(item, dict) or item.get("instanceId") != value["instanceId"]
                or not _cursor(item.get("cursor"))
                or not previous < item["cursor"] <= value["cursor"]
                or not isinstance(item.get("type"), str)
                or _TYPE.fullmatch(item["type"]) is None
                or not _identity(item.get("conversationId"))
                or not _identity(item.get("runId", ""), empty=True)):
            raise RuntimeUnavailable("Runtime event page is invalid")
        gap = gap or item["cursor"] != previous + 1
        events.append({key: item.get(key, "") for key in
                       ("instanceId", "cursor", "type", "conversationId", "runId")})
        previous = item["cursor"]
    gap = gap or (not events and value["cursor"] > after)
    return {"instanceId": value["instanceId"], "cursor": value["cursor"],
            "events": events, "gap": gap}


class AdapterEventReader:
    """One short-poll reader for one exact Node/adapter authority.

    ``rows`` rechecks current authority; ``refresh`` performs a bound
    snapshot reconciliation. Event text never enters Bridge storage/UI.
    Short long polls bound shutdown and reconnect latency.
    """

    def __init__(self, adapter_factory, rows, refresh, *, stop=None):
        self.adapter_factory = adapter_factory
        self.rows = rows
        self.refresh = refresh
        self.stop = stop or threading.Event()
        self.instance_id = None
        self.after = 0
        self.resync = True
        self.thread = None
        self.retry_at = 0.0

    def poll_once(self) -> bool:
        if self.stop.is_set() or not self.rows():
            return False
        adapter = self.adapter_factory()
        if self.instance_id is None:
            descriptor = adapter.descriptor()
            if not descriptor.supports("events"):
                raise RuntimeUnsupported("Runtime does not support events")
            self.instance_id = descriptor.instance_id
        value = adapter.events(after=self.after, wait_ms=1000)
        if self.stop.is_set():
            return False
        # A restart must be positively confirmed by the descriptor. Reset
        # only transport cursors; native snapshots decide recovered state.
        restarted = isinstance(value, dict) and value.get("instanceId") != self.instance_id
        if restarted:
            descriptor = adapter.descriptor()
            if (not descriptor.supports("events")
                    or descriptor.instance_id != value.get("instanceId")):
                raise RuntimeUnavailable("Runtime event instance changed")
            self.instance_id = descriptor.instance_id
            self.after = 0
            self.resync = True
        page = validate_event_page(value, self.after)
        rows = self.rows()
        if self.stop.is_set() or not rows:
            return False
        targets = set()
        if self.resync or page["gap"]:
            targets.update(row["id"] for row in rows)
        for event in page["events"]:
            for row in rows:
                if (event["conversationId"] == row["native_conversation"]
                        and (not event["runId"] or event["runId"] == row["native_id"])):
                    targets.add(row["id"])
        for run_id in targets:
            if self.stop.is_set():
                return False
            self.refresh(run_id)
        self.resync = False
        # On an empty gap, the full snapshot covered the missing hints.
        # Otherwise drain from the final delivered cursor, never the head.
        self.after = (page["events"][-1]["cursor"] if page["events"] else page["cursor"])
        return True

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="wbrp-events", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        delay = 0.25
        while not self.stop.is_set():
            try:
                if not self.poll_once():
                    return
                delay = 0.25
            except RuntimeUnsupported:
                # No event feature is a supported polling-only runtime.
                self.retry_at = time.monotonic() + 30
                return
            except Exception:  # noqa: BLE001 - failed hints never change run state
                self.resync = True
                delay = min(max(delay * 2, 1), 5)
            if self.stop.wait(delay):
                return

    def close(self) -> None:
        self.stop.set()

"""Event bus: every Event goes to the structured log and to all WebSocket subscribers (thread-safe fan-out)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from backend.contracts import Event, EventType
from backend.monitoring import EventLog


def to_payload(obj: Any) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


class EventBus:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self._subs: dict[int, Callable[[Event], None]] = {}
        self._lock = threading.Lock()
        self._next = 0

    def publish(self, type_: EventType, payload: Any, **extra: Any) -> Event:
        p = to_payload(payload)
        if extra:
            p = {**p, **{k: to_payload(v) for k, v in extra.items()}}
        ev = Event(type=type_, payload=p)
        self.log.write(ev)
        with self._lock:
            subs = list(self._subs.values())
        for s in subs:
            try:
                s(ev)
            except Exception:  # a dead subscriber must not break trading
                pass
        return ev

    def subscribe_async(self, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[Event]") -> int:
        def push(ev: Event) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        with self._lock:
            self._next += 1
            self._subs[self._next] = push
            return self._next

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

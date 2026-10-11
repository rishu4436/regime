"""Structured decision/execution log: every Event is kept in memory and (optionally) appended as one JSON line."""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from backend.contracts import Event


class EventLog:
    def __init__(self, path: Optional[str | os.PathLike] = None, maxlen: int = 5000) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(event.model_dump_json() + "\n")

    def events(self, type_: Optional[str] = None) -> list[Event]:
        with self._lock:
            return [e for e in self._events if type_ is None or e.type.value == type_]

    @staticmethod
    def read_jsonl(path: str | os.PathLike) -> list[Event]:
        with open(path, encoding="utf-8") as f:
            return [Event.model_validate(json.loads(line)) for line in f if line.strip()]

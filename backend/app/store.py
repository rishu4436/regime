"""Saved manual strategies (JSON). In memory, optionally persisted to `<dir>/strategies.json`."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from backend.contracts import ManualStrategy


class StrategyStore:
    def __init__(self, directory: Optional[str] = None) -> None:
        self._items: dict[str, ManualStrategy] = {}
        self._lock = threading.Lock()
        self._path = Path(directory) / "strategies.json" if directory else None
        if self._path and self._path.exists():
            for d in json.loads(self._path.read_text(encoding="utf-8")):
                s = ManualStrategy.model_validate(d)
                self._items[s.id] = s

    def _flush(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([s.model_dump(mode="json") for s in self._items.values()], indent=2), encoding="utf-8")

    def list(self) -> list[ManualStrategy]:
        with self._lock:
            return list(self._items.values())

    def get(self, sid: str) -> ManualStrategy:
        with self._lock:
            return self._items[sid]

    def save(self, s: ManualStrategy) -> ManualStrategy:
        with self._lock:
            self._items[s.id] = s
            self._flush()
            return s

    def update(self, sid: str, s: ManualStrategy) -> ManualStrategy:
        with self._lock:
            old = self._items[sid]
            s = s.model_copy(update={"id": sid, "version": old.version + 1})
            self._items[sid] = s
            self._flush()
            return s

    def delete(self, sid: str) -> None:
        with self._lock:
            del self._items[sid]
            self._flush()

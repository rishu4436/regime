"""Persistent agent state for 24/7 operation: the active agent (manual strategy / auto config), the control state
(emergency stop survives restarts), the app's LIVE daily-notional ledger and, outside SIM, the open lots.

One JSON file per trading mode (`agent_state.<mode>.json` in STATE_DIR or LOG_DIR), written atomically, so a SIM
session can never resume into LIVE. No secrets are stored."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Optional

from backend.contracts import TradingMode, utc_now

log = logging.getLogger("backend.app.state")


def state_path(state_dir: Optional[str], mode: TradingMode) -> Optional[str]:
    return os.path.join(state_dir, f"agent_state.{mode.value.lower()}.json") if state_dir else None


class AgentStateStore:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict:
        if not self.path or not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:  # a corrupt file must not stop the agent from booting (it starts clean, loudly)
            log.exception("could not read agent state %s; starting clean", self.path)
            return {"_corrupt": True}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, **kw: Any) -> None:
        with self._lock:
            self.data.update(kw)
            self.data.pop("_corrupt", None)
            self.data["updated_at"] = utc_now().isoformat()
            if not self.path:
                return
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".agent_state.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=1, default=str)
                os.replace(tmp, self.path)
            except Exception:
                log.exception("could not write agent state %s", self.path)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

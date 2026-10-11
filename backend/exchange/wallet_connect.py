"""Agentic Wallet sign-in from the dashboard ("Connect wallet"): wraps `baw auth signin` + `baw auth verify`.

Flow (baw 1.10.0, async login):
1. `start()` runs `baw auth signin --json`, which returns at once with `urlForWeb` (the QR link), `qrCodeId` and
   `pairingCode`. It then launches `baw auth verify --qrCodeId <id> --json` in the background; verify polls Binance
   every ~2 s for up to ~5 min and exits once the QR is scanned and approved in the Binance app (or it times out).
2. `poll(session_id)` reports `pending` while verify runs. When verify exits OK it checks `baw wallet status`
   (must be CONNECTED) and reads the BSC address, then returns `connected` with that address.
   `wallet_changed` is true when the address differs from `expected_address` (the wallet the agent's saved state
   belongs to). The app must keep LIVE blocked ("different wallet connected") until the saved positions are
   reconciled against the new wallet. Re-reading the wallet limits after `connected` is the app's job.

Only one sign-in runs at a time; starting a new one cancels the previous one. This module never trades and never
sees secrets: the session lives in baw's own local store. Exposing it over HTTP must be localhost-only (the app's job).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

from .baw import BSC_CHAIN_ID, BawCli, BawError, _parse_json

VERIFY_TTL_S = 300.0  # baw auth verify gives up after about 5 minutes

PENDING, CONNECTED, EXPIRED, FAILED = "pending", "connected", "expired", "failed"


class Proc(Protocol):
    def poll(self) -> Optional[int]: ...
    def communicate(self, timeout: Optional[float] = None) -> tuple[str, str]: ...
    def kill(self) -> None: ...


Launcher = Callable[[Sequence[str]], Proc]


def popen_launcher(argv: Sequence[str]) -> Proc:
    kw: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0"), **kw)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class WalletConnect:
    def __init__(self, cli: Optional[BawCli] = None, expected_address: Optional[str] = None,
                 launcher: Optional[Launcher] = None, clock: Callable[[], float] = time.time,
                 ttl_s: float = VERIFY_TTL_S) -> None:
        self.cli = cli or BawCli()
        self.expected_address = expected_address if expected_address is not None else (
            os.environ.get("BINANCE_WALLET_ADDRESS") or None)
        self.launcher = launcher or popen_launcher
        self.clock, self.ttl_s = clock, ttl_s
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._active: Optional[str] = None

    # ------------------------------------------------------------------ start
    def start(self) -> dict:
        """Begin a sign-in. Returns {session_id, qr_url, pairing_code, expires_at}. Raises BawError if signin fails."""
        with self._lock:
            self._cancel_active()
            data = self.cli.call("auth", "signin") or {}
            qr_id = str(data.get("qrCodeId") or "")
            qr_url = data.get("urlForWeb") or data.get("qrCodeUrl") or data.get("url")
            if not qr_id or not qr_url:
                raise BawError(f"baw auth signin returned no QR code: {json.dumps(data)[:200]}")
            proc = self.launcher([*self.cli.cmd, "auth", "verify", "--qrCodeId", qr_id, "--json"])
            sid = uuid.uuid4().hex
            now = self.clock()
            self._sessions[sid] = {"proc": proc, "qr_id": qr_id, "expires": now + self.ttl_s,
                                   "result": None}
            self._active = sid
            return {"session_id": sid, "qr_url": qr_url, "pairing_code": data.get("pairingCode"),
                    "expires_at": _iso(now + self.ttl_s)}

    # ------------------------------------------------------------------ poll
    def poll(self, session_id: str) -> dict:
        """{status: pending|connected|expired|failed, address, wallet_changed, error}."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return self._out(FAILED, error="unknown sign-in session")
            if s["result"] is not None:
                return s["result"]
            proc: Proc = s["proc"]
            rc = proc.poll()
            if rc is None:
                if self.clock() < s["expires"]:
                    return self._out(PENDING)
                self._kill(proc)
                return self._finish(session_id, self._out(EXPIRED, error="QR code expired before it was scanned"))
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - output is best-effort
                out, err = "", ""
            payload = _parse_json(out) or _parse_json(err)
            ok = rc == 0 and (not isinstance(payload, dict) or payload.get("success") is not False)
            if not ok:
                msg = ""
                if isinstance(payload, dict):
                    e = payload.get("error") or {}
                    msg = str(e.get("message") or e.get("name") or "")
                msg = msg or (err or out or f"baw auth verify exited {rc}").strip()[:200]
                low = msg.lower()
                status = EXPIRED if any(w in low for w in ("expire", "timeout", "timed out")) else FAILED
                return self._finish(session_id, self._out(status, error=msg))
            return self._finish(session_id, self._connected())

    def _connected(self) -> dict:
        try:
            st = self.cli.status()
            if st != "CONNECTED":
                return self._out(FAILED, error=f"wallet status is {st} after sign-in")
            addr = self.cli.address(BSC_CHAIN_ID)
        except BawError as e:
            return self._out(FAILED, error=str(e))
        changed = bool(self.expected_address and addr and addr.lower() != self.expected_address.lower())
        return self._out(CONNECTED, address=addr, wallet_changed=changed)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _out(status: str, address: Optional[str] = None, wallet_changed: bool = False,
             error: Optional[str] = None) -> dict:
        return {"status": status, "address": address, "wallet_changed": wallet_changed, "error": error}

    def _finish(self, sid: str, result: dict) -> dict:
        self._sessions[sid]["result"] = result
        if self._active == sid:
            self._active = None
        return result

    @staticmethod
    def _kill(proc: Proc) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def _cancel_active(self) -> None:
        sid = self._active
        if sid and self._sessions.get(sid, {}).get("result") is None:
            self._kill(self._sessions[sid]["proc"])
            self._finish(sid, self._out(FAILED, error="replaced by a newer sign-in"))

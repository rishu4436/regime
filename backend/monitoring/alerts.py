"""Alert rules over decisions, executions and control changes."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Optional

from backend.contracts import ControlState, Decision, ExecutionResult, OrderStatus

from .models import Alert, AlertLevel

CONSECUTIVE_LOSS_ALERT = 3


class AlertManager:
    def __init__(self, maxlen: int = 500) -> None:
        self._alerts: deque[Alert] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._last_control: Optional[ControlState] = None

    def raise_(self, level: AlertLevel, code: str, message: str, symbol: Optional[str] = None,
               timestamp: Optional[datetime] = None) -> Alert:
        a = Alert(level=level, code=code, message=message, symbol=symbol, **({"timestamp": timestamp} if timestamp else {}))
        with self._lock:
            self._alerts.append(a)
        return a

    def alerts(self, limit: int = 50) -> list[Alert]:
        with self._lock:
            return list(self._alerts)[-limit:][::-1]

    def on_control(self, c: ControlState) -> list[Alert]:
        prev, self._last_control = self._last_control, c
        out = []
        if c.emergency_stopped and not (prev and prev.emergency_stopped):
            out.append(self.raise_(AlertLevel.CRITICAL, "EMERGENCY_STOP", f"Emergency stop: {c.reason or 'no reason given'}"))
        elif c.paused and not c.emergency_stopped and not (prev and prev.paused):
            out.append(self.raise_(AlertLevel.WARNING, "PAUSED", f"Trading paused: {c.reason or 'no reason given'}"))
        return out

    def on_execution(self, r: ExecutionResult) -> list[Alert]:
        if r.status == OrderStatus.EXPIRED:
            return [self.raise_(AlertLevel.WARNING, "QUOTE_EXPIRED", f"{r.side.value} {r.symbol}: quote expired, not filled", r.symbol)]
        if r.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            return [self.raise_(AlertLevel.WARNING, "EXECUTION_FAILED", f"{r.side.value} {r.symbol}: {r.status.value} {r.error or ''}".strip(), r.symbol)]
        return []

    def on_decision(self, d: Decision) -> list[Alert]:
        v = d.risk_verdict
        if v is not None and not v.approved and d.signal is not None and d.signal.action.value != "STAY_OUT":
            return [self.raise_(AlertLevel.INFO, "RISK_BLOCKED", f"{d.signal.action.value} {d.symbol} blocked: {'; '.join(v.reasons)}", d.symbol)]
        return []

    def on_trade_closed(self, consecutive_losses: int, daily_pnl: float, daily_limit: float, symbol: str) -> list[Alert]:
        out = []
        if consecutive_losses >= CONSECUTIVE_LOSS_ALERT:
            out.append(self.raise_(AlertLevel.WARNING, "CONSECUTIVE_LOSSES", f"{consecutive_losses} losing trades in a row", symbol))
        if daily_limit > 0 and daily_pnl <= -daily_limit:
            out.append(self.raise_(AlertLevel.CRITICAL, "DAILY_LOSS", f"Daily PnL {daily_pnl:.2f} breached -{daily_limit:.2f}", symbol))
        return out

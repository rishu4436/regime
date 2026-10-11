"""Performance tracking from closed trades + a mark-to-market equity curve (fixes the simulator's understated
drawdown, which only counted realised equity)."""
from __future__ import annotations

import threading
from datetime import datetime

from backend.contracts import EquityPoint, PerformanceSummary, Portfolio


class PerformanceTracker:
    def __init__(self, max_points: int = 10000) -> None:
        self._curve: list[EquityPoint] = []
        self._pnls: list[float] = []
        self._lock = threading.Lock()
        self._max = max_points

    def record_equity(self, ts: datetime, equity: float) -> None:
        with self._lock:
            if self._curve and self._curve[-1].timestamp == ts:
                self._curve[-1] = EquityPoint(timestamp=ts, equity=equity)
            else:
                self._curve.append(EquityPoint(timestamp=ts, equity=equity))
            del self._curve[:-self._max]

    def rebase(self, ts: datetime, equity: float) -> None:
        """Start the equity curve over at `equity` (the portfolio's starting capital). Used when the book's baseline is
        set after the first point was recorded (LIVE/PAPER: restored state or the wallet balance), so drawdown and
        return never mix two baselines (e.g. a 10000 placeholder and a 109.87 USDT wallet = a fake 98.9% drawdown)."""
        with self._lock:
            self._curve = [EquityPoint(timestamp=ts, equity=equity)]

    def record_trade(self, pnl: float) -> None:
        with self._lock:
            self._pnls.append(pnl)

    @property
    def consecutive_losses(self) -> int:
        n = 0
        for p in reversed(self._pnls):
            if p >= 0:
                break
            n += 1
        return n

    @staticmethod
    def max_drawdown_pct(curve: list[EquityPoint], start: float) -> float:
        peak, mdd = start, 0.0
        for pt in curve:
            peak = max(peak, pt.equity)
            if peak > 0:
                mdd = max(mdd, (peak - pt.equity) / peak * 100)
        return mdd

    def summary(self, p: Portfolio) -> PerformanceSummary:
        with self._lock:
            curve, pnls = list(self._curve), list(self._pnls)
        wins = sum(1 for x in pnls if x > 0)
        unreal = sum(x.unrealized_pnl for x in p.positions)
        return PerformanceSummary(
            starting_capital=p.starting_capital, equity=round(p.equity, 8), realized_pnl=round(p.realized_pnl, 8),
            unrealized_pnl=round(unreal, 8), total_return_pct=round((p.equity / p.starting_capital - 1) * 100, 6),
            max_drawdown_pct=round(self.max_drawdown_pct(curve, p.starting_capital), 6),
            win_rate_pct=round(wins / len(pnls) * 100, 6) if pnls else 0.0, num_trades=len(pnls), wins=wins,
            losses=len(pnls) - wins, consecutive_losses=self.consecutive_losses, equity_curve=curve,
        )

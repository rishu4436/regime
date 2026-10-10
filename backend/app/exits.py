"""Agreed protective-exit semantics (team decision, applies to Engine.backtest_manual and Risk.check_exits).

Per bar, for an open position (never on its entry bar):
1. Levels are taken from the PREVIOUS bar's state: stop-loss price, take-profit price and the trailing level
   `highest_before_this_bar * (1 - trail_pct/100)` (highest starts at the entry price).
2. The active stop is the higher of trailing level and stop-loss price (tie -> TRAILING_STOP, simulator priority).
3. Gaps: if the bar OPENS at/through the stop -> exit at the open (STOP_LOSS / TRAILING_STOP);
   if it OPENS at/through the take-profit -> exit at the open (TAKE_PROFIT).
4. Intrabar: low <= stop -> exit at the stop level; high >= take-profit -> exit at the take-profit level.
   If both are touched in the same bar the STOP wins (pessimistic: OHLC cannot tell which came first).
5. After the checks, the trailing high-water mark is raised with this bar's HIGH (used from the next bar on).
RSI / MACD exits stay on the bar close and are the engine's job (evaluated after the protective exits).

This module is the app's executable statement of that rule: the fallback engine/risk use it, and the parity
tests in tests/e2e assert the real Bot 2 / Bot 3 implementations agree with it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.contracts import ExitReason

STOP_WINS_SAME_BAR = True  # documented assumption for "SL and TP touched in the same bar"


@dataclass(frozen=True)
class ExitHit:
    reason: ExitReason
    price: float


def trailing_level(highest: Optional[float], trail_pct: Optional[float]) -> Optional[float]:
    if highest is None or not trail_pct:
        return None
    return highest * (1 - trail_pct / 100.0)


def check_protective_exit(
    *,
    open_: float,
    high: float,
    low: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    trail_level: Optional[float],
) -> Optional[ExitHit]:
    stops: list[tuple[float, ExitReason]] = []
    if trail_level is not None:
        stops.append((trail_level, ExitReason.TRAILING_STOP))
    if stop_loss_price is not None:
        stops.append((stop_loss_price, ExitReason.STOP_LOSS))
    stop = None
    if stops:
        # highest level is hit first; on a tie the trailing stop is reported (listed first, max keeps the first)
        stop = max(stops, key=lambda s: s[0])
    tp = take_profit_price
    if stop is not None and open_ <= stop[0]:
        return ExitHit(stop[1], open_)
    if tp is not None and open_ >= tp:
        return ExitHit(ExitReason.TAKE_PROFIT, open_)
    stop_hit = stop is not None and low <= stop[0]
    tp_hit = tp is not None and high >= tp
    if stop_hit and (STOP_WINS_SAME_BAR or not tp_hit):
        return ExitHit(stop[1], stop[0])
    if tp_hit:
        return ExitHit(ExitReason.TAKE_PROFIT, tp)
    if stop_hit:
        return ExitHit(stop[1], stop[0])
    return None


def exit_reason_of(signal) -> Optional[ExitReason]:
    """Exit reason of a SELL Signal: `Signal.exit_reason` (contract v0.2); if an implementation leaves it empty,
    fall back to an ExitReason name in the reason text (for logging only)."""
    if signal.exit_reason is not None:
        return ExitReason(signal.exit_reason)
    text = (signal.reason or "").upper().replace("-", "_")
    for r in ExitReason:
        if r.value in text or r.value.replace("_", " ") in text:
            return r
    return None

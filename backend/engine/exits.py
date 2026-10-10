"""Intrabar price-exit rules shared by backtest_manual and evaluate_manual (agreed with Bot 3's check_exits).

Rules for one open position on one bar (the entry bar itself is never checked: entries fill at that bar's close):

1. Stop level = the HIGHEST of: stop-loss level ``entry*(1-sl/100)``, trailing level CARRIED FROM THE PREVIOUS BAR
   (if enabled) and the max-loss level (if ``risk.max_loss_per_trade_pct`` is set). Reason = the rule that owns
   the highest level; on an exact tie the order is TRAILING_STOP > STOP_LOSS > MAX_LOSS.
2. Gap down: ``open <= stop``  -> exit at the OPEN with the stop's reason.
3. Gap up:   ``open >= tp``    -> exit at the OPEN with TAKE_PROFIT.
4. ``low <= stop``             -> exit at the stop level (if ``high >= tp`` too, the stop still wins: conservative).
5. ``high >= tp``              -> exit at the take-profit level ``entry*(1+tp/100)``.
6. No exit -> ratchet the trailing level with THIS bar's high: ``trail = max(trail, high*(1-pct/100))``
   (used from the next bar on).
Indicator exits (RSI, MACD) are evaluated afterwards on the close and fill at the close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.contracts import ExitReason


@dataclass(frozen=True)
class PriceExit:
    reason: ExitReason
    price: float
    gap: bool = False


def stop_level(
    entry_price: float,
    stop_loss_pct: float,
    trail_level: Optional[float] = None,
    max_loss_level: Optional[float] = None,
) -> tuple[float, ExitReason]:
    """Highest active stop and its reason (tie order TRAILING_STOP > STOP_LOSS > MAX_LOSS)."""
    candidates: list[tuple[float, int, ExitReason]] = [
        (entry_price * (1 - stop_loss_pct / 100), 1, ExitReason.STOP_LOSS)
    ]
    if trail_level is not None:
        candidates.append((trail_level, 2, ExitReason.TRAILING_STOP))
    if max_loss_level is not None:
        candidates.append((max_loss_level, 0, ExitReason.MAX_LOSS))
    level, _, reason = max(candidates, key=lambda c: (c[0], c[1]))
    return level, reason


def check_price_exit(
    open_: float,
    high: float,
    low: float,
    entry_price: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    trail_level: Optional[float] = None,
    max_loss_level: Optional[float] = None,
) -> Optional[PriceExit]:
    """Apply rules 1-5 above to one bar. Returns None when no price exit triggers."""
    stop, stop_reason = stop_level(entry_price, stop_loss_pct, trail_level, max_loss_level)
    tp = entry_price * (1 + take_profit_pct / 100)
    if open_ <= stop:
        return PriceExit(stop_reason, open_, gap=True)
    if open_ >= tp:
        return PriceExit(ExitReason.TAKE_PROFIT, open_, gap=True)
    if low <= stop:
        return PriceExit(stop_reason, stop)
    if high >= tp:
        return PriceExit(ExitReason.TAKE_PROFIT, tp)
    return None


def initial_trail(entry_price: float, trail_pct: float) -> float:
    return entry_price * (1 - trail_pct / 100)


def ratchet_trail(trail_level: float, bar_high: float, trail_pct: float) -> float:
    return max(trail_level, bar_high * (1 - trail_pct / 100))


def max_loss_level(
    entry_price: float, quantity: float, capital: float, max_loss_pct: Optional[float]
) -> Optional[float]:
    """Price at which the position loses ``max_loss_pct`` % of ``capital`` (equity at entry). None when disabled."""
    if max_loss_pct is None or quantity <= 0:
        return None
    level = entry_price - capital * max_loss_pct / 100 / quantity
    return level if level > 0 else None

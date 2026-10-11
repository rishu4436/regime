"""Intrabar price-exit rules shared by backtest_manual and evaluate_manual.

Implements the ``ExitRules`` docstring (contracts v0.2) and is kept identical to Bot 3's ``backend/risk/exits.py``
(``evaluate_bar``) so the backtest and the live ``RiskManager.check_exits`` path agree on exit bar, reason and fill.
Rules for one open position on one bar (the entry bar itself is never checked: entries fill at that bar's close):

1. Hit tests, each against its own level:
   - stop loss   ``low <= entry*(1-sl/100)``
   - trailing    ``low <  trail`` (STRICT), ``trail = hwm_prev*(1-pct/100)`` where ``hwm_prev`` is the highest high of
     the PRIOR bars since entry (starts at the entry price), so a bar's own high never tightens its own stop
   - max loss    ``low <= entry - capital*max_loss_pct/100/qty`` (``capital`` = starting capital; off by default)
   - take profit ``high >= entry*(1+tp/100)``
2. If ANY stop is hit, the stop wins over the take profit (even if TP is also touched, even on a gap-up open).
   Among the hit stops the HIGHEST level wins (it is touched first on the way down); exact ties resolve
   TRAILING_STOP > STOP_LOSS > MAX_LOSS. Fill = the level, or the OPEN if ``open < level`` (gap down).
3. Otherwise, if TP is hit: fill = the TP level, or the OPEN if ``open > level`` (gap up).
4. No exit: the high-water mark becomes ``max(hwm_prev, bar.high)`` for the next bar.
Indicator exits (RSI, MACD) are evaluated afterwards on the close and fill at the close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.contracts import ExitReason

_STOP_PRIORITY = {ExitReason.TRAILING_STOP: 0, ExitReason.STOP_LOSS: 1, ExitReason.MAX_LOSS: 2}


@dataclass(frozen=True)
class PriceExit:
    reason: ExitReason
    price: float
    gap: bool = False
    level: float = 0.0


def check_price_exit(
    open_: float,
    high: float,
    low: float,
    entry_price: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    trail_level: Optional[float] = None,
    max_loss_level: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
) -> Optional[PriceExit]:
    """Apply rules 1-3 above to one bar. ``trail_level`` must come from PRIOR bars. Explicit
    ``stop_loss_price`` / ``take_profit_price`` (e.g. from ``Position``) override the percentage levels."""
    sl = stop_loss_price if stop_loss_price is not None else entry_price * (1 - stop_loss_pct / 100)
    tp = take_profit_price if take_profit_price is not None else entry_price * (1 + take_profit_pct / 100)
    hit: list[tuple[float, ExitReason]] = []
    if low <= sl:
        hit.append((sl, ExitReason.STOP_LOSS))
    if trail_level is not None and low < trail_level:
        hit.append((trail_level, ExitReason.TRAILING_STOP))
    if max_loss_level is not None and low <= max_loss_level:
        hit.append((max_loss_level, ExitReason.MAX_LOSS))
    if hit:
        level, reason = min(hit, key=lambda h: (-h[0], _STOP_PRIORITY[h[1]]))
        gap = open_ < level
        return PriceExit(reason, open_ if gap else level, gap=gap, level=level)
    if high >= tp:
        gap = open_ > tp
        return PriceExit(ExitReason.TAKE_PROFIT, open_ if gap else tp, gap=gap, level=tp)
    return None


def trail_from_hwm(hwm_prev: float, trail_pct: float) -> float:
    """Trailing level in force for the current bar, from the high-water mark of PRIOR bars."""
    return hwm_prev * (1 - trail_pct / 100)


def max_loss_level(
    entry_price: float, quantity: float, capital: float, max_loss_pct: Optional[float]
) -> Optional[float]:
    """Price at which the position has lost ``max_loss_pct`` % of ``capital`` (starting capital, as Bot 3).
    None when disabled or unreachable (level <= 0)."""
    if not max_loss_pct or quantity <= 0 or capital <= 0:
        return None
    level = entry_price - capital * max_loss_pct / 100 / quantity
    return level if level > 0 else None

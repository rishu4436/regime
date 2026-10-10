"""Pure exit-rule evaluation for one position on one bar (no state, no I/O).

Exit semantics agreed with Bot 2's ``backtest_manual`` (keep both in sync):

1. Stop loss and take profit are checked against the bar's LOW / HIGH. If a stop and the take profit are both hit in
   the same bar, the stop wins (we cannot know the intrabar order, so we assume the worst).
2. Trailing stop: the bar's LOW is checked against the trail level computed from the high-water mark of the PREVIOUS
   bars (``hwm_prev * (1 - pct/100)``); only if the position survives is the high-water mark raised with this bar's HIGH.
   The first trail level is ``entry * (1 - pct/100)`` (high-water mark starts at the entry price).
3. Exits fill at the stop / target level, not at the close. If the bar OPENS beyond the level (gap down through a stop,
   gap up through the target) the fill is the open.

With a price only (no candle) the bar is degenerate (open = high = low = close = price), which reduces to a classic
"last price crossed the level -> exit at the last price" check.

Hit tests: stop loss ``low <= stop``; trailing ``low < trail`` (strict, like the simulator's ``close < trail``);
max-loss ``low <= level``; take profit ``high >= target``. When several stops are hit in one bar the HIGHEST level is the
one touched first on the way down and is used for both the fill and the reason; ties resolve in simulator priority
order TRAILING_STOP > STOP_LOSS > MAX_LOSS.
RSI / MACD exits need indicators and belong to the strategy engine (Bot 2), not to the risk layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.contracts import Candle, ExitReason, ExitRules, Position


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_price(cls, price: float) -> "Bar":
        return cls(price, price, price, price)

    @classmethod
    def from_candle(cls, c: Candle) -> "Bar":
        return cls(c.open, c.high, c.low, c.close)


@dataclass(frozen=True)
class ExitLevels:
    entry: float
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float]  # level in force for THIS bar (from previous high-water mark)
    trailing_pct: Optional[float]
    max_loss: Optional[float]
    high_water_mark: float


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    level: float
    fill_price: float
    gapped: bool
    pnl_pct: float
    detail: str


_STOP_PRIORITY = {ExitReason.TRAILING_STOP: 0, ExitReason.STOP_LOSS: 1, ExitReason.MAX_LOSS: 2}


def max_loss_level(position: Position, max_loss_per_trade_pct: Optional[float], capital: float) -> Optional[float]:
    """Price at which the position has lost ``max_loss_per_trade_pct`` % of ``capital`` (USDT). None if off/unreachable."""
    if not max_loss_per_trade_pct or capital <= 0:
        return None
    level = position.avg_entry_price - (capital * max_loss_per_trade_pct / 100.0) / position.quantity
    return level if level > 0 else None


def compute_levels(position: Position, rules: ExitRules, hwm_prev: float, max_loss: Optional[float] = None) -> ExitLevels:
    entry = position.avg_entry_price
    sl = position.stop_loss_price or entry * (1 - rules.stop_loss_pct / 100.0)
    tp = position.take_profit_price or entry * (1 + rules.take_profit_pct / 100.0)
    pct = position.trailing_stop_pct or (rules.trailing_stop.pct if rules.trailing_stop.enabled else None)
    trail = hwm_prev * (1 - pct / 100.0) if pct else None
    return ExitLevels(entry, sl, tp, trail, pct, max_loss, hwm_prev)


def evaluate_bar(levels: ExitLevels, bar: Bar) -> tuple[Optional[ExitDecision], float]:
    """Return (exit decision or None, new high-water mark)."""
    entry = levels.entry
    stops: list[tuple[float, ExitReason]] = []
    if bar.low <= levels.stop_loss:
        stops.append((levels.stop_loss, ExitReason.STOP_LOSS))
    if levels.trailing_stop is not None and bar.low < levels.trailing_stop:
        stops.append((levels.trailing_stop, ExitReason.TRAILING_STOP))
    if levels.max_loss is not None and bar.low <= levels.max_loss:
        stops.append((levels.max_loss, ExitReason.MAX_LOSS))

    if stops:
        level, reason = sorted(stops, key=lambda s: (-s[0], _STOP_PRIORITY[s[1]]))[0]
        gapped = bar.open < level
        fill = bar.open if gapped else level
        tp_also = bar.high >= levels.take_profit
        pnl = (fill - entry) / entry * 100.0
        name = {ExitReason.STOP_LOSS: "stop loss", ExitReason.TRAILING_STOP: "trailing stop",
                ExitReason.MAX_LOSS: "max loss per trade"}[reason]
        detail = (f"{reason.value}: low {bar.low:.4f} reached {name} {level:.4f} (entry {entry:.4f}); "
                  + (f"gapped - opened at {bar.open:.4f}, filled at the open" if gapped else f"filled at {fill:.4f}")
                  + f"; PnL {pnl:+.2f}%")
        if reason == ExitReason.TRAILING_STOP:
            detail += f"; trail = high-water mark {levels.high_water_mark:.4f} - {levels.trailing_pct}%"
        if tp_also:
            detail += f"; take profit {levels.take_profit:.4f} also touched this bar - stop wins (worst case)"
        return ExitDecision(reason, level, fill, gapped, pnl, detail), levels.high_water_mark

    if bar.high >= levels.take_profit:
        level = levels.take_profit
        gapped = bar.open > level
        fill = bar.open if gapped else level
        pnl = (fill - entry) / entry * 100.0
        detail = (f"TAKE_PROFIT: high {bar.high:.4f} reached target {level:.4f} (entry {entry:.4f}); "
                  + (f"gapped - opened at {bar.open:.4f}, filled at the open" if gapped else f"filled at {fill:.4f}")
                  + f"; PnL {pnl:+.2f}%")
        return ExitDecision(ExitReason.TAKE_PROFIT, level, fill, gapped, pnl, detail), levels.high_water_mark

    return None, max(levels.high_water_mark, bar.high)

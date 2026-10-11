"""Agreed protective-exit semantics (contract v0.2 ExitRules + Bot 3 edge rules; Engine.backtest_manual and
Risk.check_exits must agree - parity-tested in tests/e2e/test_exit_parity.py).

Per bar, for an open position (never on its entry bar):
1. Levels come from PREVIOUS bars: stop-loss price, take-profit price, optional max-loss level, and the trailing level
   `max(highest_price, entry) * (1 - trail_pct/100)` where highest_price only includes bars before this one.
2. Stop hit tests: stop-loss `low <= level`; trailing `low < level` (STRICT); max-loss `low <= level`.
3. If any stop is hit it wins over a take-profit touched in the same bar (worst case; this includes a bar that gaps
   up through the TP and then trades down through a stop). Among several hit stops the HIGHEST level wins
   (it is touched first on the way down); ties: TRAILING_STOP > STOP_LOSS > MAX_LOSS.
4. Fill at the level, or at the OPEN if the bar opened beyond it (stop: open < level -> open; TP: open > level -> open).
5. Take-profit: `high >= level`, only if no stop was hit.
6. After check_exits, the app raises Position.highest_price with this bar's HIGH (check_exits never mutates).
7. Emergency stop: risk emits EMERGENCY_STOP exits for every position filled at the bar close / last price.
RSI / MACD exits stay on the bar close and are the engine's job (evaluated after the protective exits).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.contracts import ExitReason

_STOP_PRIORITY = {ExitReason.TRAILING_STOP: 0, ExitReason.STOP_LOSS: 1, ExitReason.MAX_LOSS: 2}


@dataclass(frozen=True)
class ExitHit:
    reason: ExitReason
    price: float


def trailing_level(highest: Optional[float], trail_pct: Optional[float], entry: Optional[float] = None) -> Optional[float]:
    if not trail_pct:
        return None
    hwm = max(highest or 0.0, entry or 0.0)
    if hwm <= 0:
        return None
    return hwm * (1 - trail_pct / 100.0)


def max_loss_level(entry: float, quantity: float, max_loss_pct: Optional[float], capital: float) -> Optional[float]:
    """Price at which the position has lost `max_loss_pct` % of `capital` (USDT); None if off / unreachable."""
    if not max_loss_pct or capital <= 0 or quantity <= 0:
        return None
    level = entry - capital * max_loss_pct / 100.0 / quantity
    return level if level > 0 else None


def check_protective_exit(
    *,
    open_: float,
    high: float,
    low: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    trail_level: Optional[float],
    max_loss_price: Optional[float] = None,
) -> Optional[ExitHit]:
    stops: list[tuple[float, ExitReason]] = []
    if stop_loss_price is not None and low <= stop_loss_price:
        stops.append((stop_loss_price, ExitReason.STOP_LOSS))
    if trail_level is not None and low < trail_level:
        stops.append((trail_level, ExitReason.TRAILING_STOP))
    if max_loss_price is not None and low <= max_loss_price:
        stops.append((max_loss_price, ExitReason.MAX_LOSS))
    if stops:
        level, reason = sorted(stops, key=lambda s: (-s[0], _STOP_PRIORITY[s[1]]))[0]
        return ExitHit(reason, open_ if open_ < level else level)
    tp = take_profit_price
    if tp is not None and high >= tp:
        return ExitHit(ExitReason.TAKE_PROFIT, open_ if open_ > tp else tp)
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

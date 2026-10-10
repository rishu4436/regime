"""Test helpers: hand-built candles and minimal strategies with predictable entries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from backend.contracts import Candle, ManualStrategy

START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)  # a Monday


def bar(i: int, o: float, h: float, low: float, c: float, v: float = 1000.0, start: datetime = START) -> Candle:
    return Candle(timestamp=start + timedelta(hours=i), open=o, high=h, low=low, close=c, volume=v)


def flat(n: int, price: float = 100.0, v: float = 1000.0, start: datetime = START) -> list[Candle]:
    return [bar(i, price, price, price, price, v, start) for i in range(n)]


ENTRY_BAR = 60
ENTRY_PRICE = 101.0


def entry_scenario(
    after: Sequence[tuple[float, float, float, float]], tail: int = 3, entry_low: Optional[float] = None
) -> list[Candle]:
    """60 flat bars at 100, an entry bar closing at 101 (> SMA(2)), then the given (o, h, l, c) bars, then a
    flat tail at the last close so nothing else happens before END."""
    cs = flat(ENTRY_BAR)
    cs.append(bar(ENTRY_BAR, 100.0, 101.0, entry_low if entry_low is not None else 100.0, ENTRY_PRICE))
    for k, (o, h, low, c) in enumerate(after):
        cs.append(bar(ENTRY_BAR + 1 + k, o, h, low, c))
    last = cs[-1].close
    for _ in range(tail):
        cs.append(bar(len(cs), last, last, last, last))
    return cs


def sma_entry_strategy(**exit_overrides) -> ManualStrategy:
    """Only 'close > SMA(2)' as entry; RSI exit off; huge cooldown -> exactly one trade per scenario."""
    s = ManualStrategy(name="test", symbol="NVDAon")
    s.entry.rsi_below.enabled = False
    s.entry.price_above_sma.period = 2
    s.exit.rsi_above.enabled = False
    s.risk.cooldown_bars = 1000
    s.risk.position_size_pct = 50.0
    for k, v in exit_overrides.items():
        setattr(s.exit, k, v)
    return s

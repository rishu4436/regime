"""Deterministic (seeded) mock candle generator that can produce each of the six regime shapes."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from backend.contracts import Candle, Regime, Timeframe

TIMEFRAME_DELTA = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}

# per-bar drift (fraction) and noise (fraction std-dev) per regime
_SHAPES: dict[Regime, tuple[float, float]] = {
    Regime.STRONG_UPTREND: (0.004, 0.003),
    Regime.MILD_UPTREND: (0.0015, 0.004),
    Regime.SIDEWAYS: (0.0, 0.002),
    Regime.OVERSOLD_PULLBACK: (0.001, 0.004),  # mild up, then a sharp drop over the last bars
    Regime.HIGH_VOLATILITY: (0.0, 0.03),
    Regime.DOWNTREND: (-0.004, 0.004),
}

DEFAULT_START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


def generate_candles(
    regime: Regime = Regime.SIDEWAYS,
    n: int = 250,
    seed: int = 42,
    start_price: float = 100.0,
    timeframe: Timeframe = Timeframe.H1,
    start_time: datetime = DEFAULT_START,
) -> list[Candle]:
    """Return `n` candles (oldest first) whose shape matches `regime`. Same args -> identical output."""
    rng = random.Random(f"{seed}:{regime.value}:{n}:{start_price}")
    drift, noise = _SHAPES[regime]
    step = TIMEFRAME_DELTA[timeframe]
    drop_bars = max(8, n // 20)
    candles: list[Candle] = []
    price = start_price
    for i in range(n):
        d = drift
        if regime == Regime.SIDEWAYS:
            d = 0.004 * math.sin(i / 6.0) - 0.004 * math.sin((i - 1) / 6.0)  # oscillate around a flat mean
        if regime == Regime.OVERSOLD_PULLBACK and i >= n - drop_bars:
            d = -0.02  # sharp drop -> low RSI
        open_ = price
        close = max(0.01, open_ * (1 + d + rng.gauss(0, noise)))
        wick = abs(rng.gauss(0, noise * 0.6)) + 0.0005
        high = max(open_, close) * (1 + wick)
        low = min(open_, close) * (1 - wick)
        base_vol = 5000.0 * (3.0 if regime == Regime.HIGH_VOLATILITY else 1.0)
        volume = base_vol * (0.5 + rng.random()) * (1.8 if regime == Regime.OVERSOLD_PULLBACK and i >= n - drop_bars else 1.0)
        candles.append(
            Candle(timestamp=start_time + i * step, open=round(open_, 6), high=round(high, 6),
                   low=round(low, 6), close=round(close, 6), volume=round(volume, 2))
        )
        price = close
    return candles

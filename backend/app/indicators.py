"""Small, dependency-free indicator set used ONLY by the app's fallback engine (until Bot 2's engine lands).
Standard formulas: SMA; EMA seeded by SMA; Wilder RSI (no `loss || 1` quirk); MACD with a true signal EMA;
Bollinger with population std-dev. Each returns a list aligned with the input, None during warm-up."""
from __future__ import annotations

import math
from typing import Optional, Sequence

Series = list[Optional[float]]


def sma(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: Sequence[Optional[float]], period: int) -> Series:
    out: Series = [None] * len(values)
    k = 2.0 / (period + 1)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < period:
        return out
    seed_end = start + period - 1
    prev = sum(values[start:seed_end + 1]) / period  # type: ignore[arg-type]
    out[seed_end] = prev
    for i in range(seed_end + 1, len(values)):
        prev = values[i] * k + prev * (1 - k)  # type: ignore[operator]
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period

    def val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100.0 - 100.0 / (1 + ag / al)

    out[period] = val(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = val(ag, al)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series]:
    f, s = ema(closes, fast), ema(closes, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    return line, ema(line, signal)


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, period)
    lo: Series = [None] * len(closes)
    up: Series = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        w = closes[i - period + 1:i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        lo[i], up[i] = m - k * sd, m + k * sd
    return lo, mid, up

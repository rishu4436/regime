"""Pure, causal technical indicators.

Every function takes a plain sequence of floats (oldest first) and returns a list of the same length where
index ``i`` only depends on inputs ``0..i`` (no look-ahead). Warm-up slots are ``None``. Callers must test
``value is None`` - never truthiness - because 0.0 is a perfectly valid indicator value (e.g. a MACD line).
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

Series = list[Optional[float]]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average of the last ``period`` values (rolling sum, O(n))."""
    _check_period(period)
    out: Series = []
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= period:
            window_sum -= values[i - period]
        out.append(window_sum / period if i >= period - 1 else None)
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """EMA with k = 2/(period+1), seeded with the SMA of the first ``period`` values (first value at index period-1)."""
    _check_period(period)
    k = 2.0 / (period + 1)
    out: Series = []
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if i < period - 1:
            out.append(None)
            continue
        if prev is None:
            prev = sum(values[i - period + 1 : i + 1]) / period
        else:
            prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def _ema_optional(values: Sequence[Optional[float]], period: int) -> Series:
    """EMA over a series with a leading block of ``None`` (e.g. a MACD line). SMA-seeded on the first
    ``period`` non-None values."""
    out: Series = [None] * len(values)
    try:
        first = next(i for i, v in enumerate(values) if v is not None)
    except StopIteration:
        return out
    tail = values[first:]
    if any(v is None for v in tail):
        raise ValueError("None values are only allowed as a leading warm-up block")
    for j, v in enumerate(ema([float(x) for x in tail], period)):  # type: ignore[arg-type]
        out[first + j] = v
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Standard Wilder RSI.

    Seed: avg gain / avg loss = arithmetic MEAN of the first ``period`` changes (first value at index ``period``).
    Then avg = (prev_avg*(period-1) + current)/period. RSI = 100 when avg loss == 0 and avg gain > 0;
    RSI = 50 when both are 0 (perfectly flat prices: neither overbought nor oversold).
    """
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        ch = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_simulator(values: Sequence[float], period: int = 14) -> Series:
    """The reference simulator's (buggy) RSI, kept ONLY for parity tests/documentation. Do not trade on it.
    It seeds with SUMS of gains/losses, then smooths them as if they were averages, and uses ``loss || 1``."""
    out: Series = []
    g = l_ = 0.0
    for i, _ in enumerate(values):
        if i == 0:
            out.append(None)
            continue
        ch = values[i] - values[i - 1]
        if i <= period:
            if ch > 0:
                g += ch
            else:
                l_ -= ch
            out.append(100 - 100 / (1 + g / (l_ or 1)) if i == period else None)
        else:
            g = (g * (period - 1) + max(ch, 0)) / period
            l_ = (l_ * (period - 1) + max(-ch, 0)) / period
            out.append(100 - 100 / (1 + g / (l_ or 1)))
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    """MACD line = EMA(fast) - EMA(slow); signal = true EMA(signal) of the line (alpha = 2/(signal+1)), seeded
    with the SMA of the first ``signal`` line values; histogram = line - signal. Returns (line, signal, hist)."""
    if fast >= slow:
        raise ValueError("MACD fast period must be < slow period")
    ef, es = ema(values, fast), ema(values, slow)
    line: Series = [None if (a is None or b is None) else a - b for a, b in zip(ef, es, strict=True)]
    sig = _ema_optional(line, signal)
    hist: Series = [None if (a is None or b is None) else a - b for a, b in zip(line, sig, strict=True)]
    return line, sig, hist


def bollinger(values: Sequence[float], period: int = 20, std_dev: float = 2.0) -> tuple[Series, Series, Series]:
    """Bollinger bands: middle = SMA(period), upper/lower = middle +/- std_dev * POPULATION std-dev.
    Returns (lower, middle, upper)."""
    mid = sma(values, period)
    lower: Series = []
    upper: Series = []
    for i, m in enumerate(mid):
        if m is None:
            lower.append(None)
            upper.append(None)
            continue
        window = values[i - period + 1 : i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        lower.append(m - std_dev * sd)
        upper.append(m + std_dev * sd)
    return lower, mid, upper


def volume_average(volumes: Sequence[float], period: Optional[int] = 20) -> Series:
    """Rolling mean volume over the last ``period`` bars INCLUDING the current one (causal).
    ``period=None`` reproduces the simulator: one average over the whole series (look-ahead in backtests)."""
    if period is None:
        if not volumes:
            return []
        avg = sum(volumes) / len(volumes)
        return [avg] * len(volumes)
    return sma(volumes, period)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    """Wilder Average True Range (seed = mean of the first ``period`` true ranges, first value at index period)."""
    _check_period(period)
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period + 1, n):
        a = (a * (period - 1) + trs[i - 1]) / period
        out[i] = a
    return out


def rolling_max(values: Sequence[float], lookback: int, include_current: bool = False) -> Series:
    """Max of the previous ``lookback`` values (excluding index i unless include_current)."""
    out: Series = []
    for i in range(len(values)):
        end = i + 1 if include_current else i
        start = end - lookback
        out.append(max(values[start:end]) if start >= 0 else None)
    return out


def rolling_min(values: Sequence[float], lookback: int, include_current: bool = False) -> Series:
    """Min of the previous ``lookback`` values (excluding index i unless include_current)."""
    out: Series = []
    for i in range(len(values)):
        end = i + 1 if include_current else i
        start = end - lookback
        out.append(min(values[start:end]) if start >= 0 else None)
    return out


def crossed_above(a: Series, b: Series, i: int) -> Optional[bool]:
    """a crosses above b at i: a[i] > b[i] and a[i-1] <= b[i-1]. None if any value is unavailable."""
    if i < 1:
        return None
    vals = (a[i], b[i], a[i - 1], b[i - 1])
    if any(v is None for v in vals):
        return None
    return a[i] > b[i] and a[i - 1] <= b[i - 1]  # type: ignore[operator]


def crossed_below(a: Series, b: Series, i: int) -> Optional[bool]:
    """a crosses below b at i: a[i] < b[i] and a[i-1] > b[i-1]. None if any value is unavailable."""
    if i < 1:
        return None
    vals = (a[i], b[i], a[i - 1], b[i - 1])
    if any(v is None for v in vals):
        return None
    return a[i] < b[i] and a[i - 1] > b[i - 1]  # type: ignore[operator]

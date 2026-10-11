"""Time-aligned SIM market data: one price path per symbol, shared by every timeframe.

The H1 series is the coordinator's seeded mock (`generate_candles(..., Timeframe.H1)`), unchanged. Every other
timeframe is derived from it on the same clock, so a bar labelled T has the same close on every timeframe:
- 1m / 5m / 15m cover the H1 span (first to last H1 bar). 1m is a seeded Brownian bridge between consecutive H1
  closes (kept inside each hour's high/low); 5m / 15m aggregate 1m.
- 4h / 1d end at the last H1 bar (floored to their grid) and have n bars. Bars fully inside the H1 span aggregate
  the H1 bars; older ones are a seeded walk with the regime's per-bar shape that ends exactly where H1 starts.
Labels follow the mocks: a bar labelled T closes at the price "at T", and its open is the previous bar's close.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from backend.contracts import Candle, Regime, Timeframe
from backend.mocks.candles import _SHAPES, DEFAULT_START, TIMEFRAME_DELTA, generate_candles

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def timeline(tf: Timeframe, n: int, start: datetime = DEFAULT_START) -> tuple[datetime, timedelta, int]:
    """(first label, step, bar count) of timeframe `tf` for an n-bar H1 replay starting at `start`."""
    step = TIMEFRAME_DELTA[tf]
    if step == HOUR:
        return start, step, n
    if step < HOUR:
        r = HOUR // step
        return start, step, (n - 1) * r + 1
    end = start + (n - 1) * HOUR
    end_tf = start + ((end - start) // step) * step
    return end_tf - (n - 1) * step, step, n


def index_at(tf: Timeframe, n: int, t: datetime, start: datetime = DEFAULT_START) -> int:
    """Index of the last `tf` bar labelled <= t (-1 if t is before the first bar)."""
    t0, step, count = timeline(tf, n, start)
    if t < t0:
        return -1
    return min(count - 1, (t - t0) // step)


def _c(ts: datetime, o: float, h: float, l: float, c: float, v: float) -> Candle:
    return Candle.model_construct(timestamp=ts, open=round(o, 6), high=round(h, 6), low=round(l, 6),
                                  close=round(c, 6), volume=round(v, 2))


class SymbolSeries:
    """All timeframes of one (symbol, regime, start price, seed, n) on a shared clock. Built lazily, then cached."""

    def __init__(self, symbol: str, regime: Regime, start_price: float, seed: int, n: int) -> None:
        self.symbol, self.regime, self.seed, self.n = symbol, regime, seed, n
        self.h1 = generate_candles(regime, n=n, seed=seed, start_price=start_price, timeframe=Timeframe.H1)
        self.start = self.h1[0].timestamp
        self._m1: list[Candle] | None = None
        self._cache: dict[Timeframe, list[Candle]] = {Timeframe.H1: self.h1}

    # ------------------------------------------------------------------ prices
    def price_at(self, t: datetime) -> float:
        """Close of the finest bar labelled <= t (H1 open before the series, last close after it)."""
        if t < self.start:
            return self.h1[0].open
        minutes = (t - self.start) // MINUTE
        if minutes % 60 == 0 or minutes >= (self.n - 1) * 60:
            return self.h1[min(self.n - 1, minutes // 60)].close
        return self.m1()[minutes].close

    # ------------------------------------------------------------------ series
    def candles(self, tf: Timeframe) -> list[Candle]:
        if tf not in self._cache:
            step = TIMEFRAME_DELTA[tf]
            self._cache[tf] = self.m1() if step == MINUTE else self._fine(tf) if step < HOUR else self._coarse(tf)
        return self._cache[tf]

    def m1(self) -> list[Candle]:
        if self._m1 is None:
            rng = random.Random(f"{self.seed}:{self.symbol}:{self.regime.value}:{self.n}:m1")
            sigma = max(_SHAPES[self.regime][1], 0.001) / math.sqrt(60)
            first = self.h1[0]
            out = [_c(first.timestamp, first.open, first.high, first.low, first.close, first.volume / 60)]
            for k in range(1, self.n):
                bar, prev_close = self.h1[k], self.h1[k - 1].close
                lo, hi = math.log(prev_close), math.log(bar.close)
                walk, w = [0.0], 0.0
                for _ in range(60):
                    w += rng.gauss(0, sigma)
                    walk.append(w)
                o = prev_close
                for j in range(1, 61):
                    if j == 60:
                        px = bar.close
                    else:
                        px = math.exp(lo + (hi - lo) * j / 60 + walk[j] - walk[60] * j / 60)
                        px = min(bar.high, max(bar.low, px))
                    wick = abs(rng.gauss(0, sigma * 0.5))
                    h = min(bar.high, max(o, px) * (1 + wick))
                    l = max(bar.low, min(o, px) * (1 - wick))
                    out.append(_c(bar.timestamp - HOUR + j * MINUTE, o, h, l, px, bar.volume / 60 * (0.5 + rng.random())))
                    o = px
            self._m1 = out
        return self._m1

    def _fine(self, tf: Timeframe) -> list[Candle]:
        m1, r = self.m1(), TIMEFRAME_DELTA[tf] // MINUTE
        out = [m1[0]]
        for k in range(1, (len(m1) - 1) // r + 1):
            chunk = m1[(k - 1) * r + 1: k * r + 1]
            out.append(_c(chunk[-1].timestamp, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk),
                          chunk[-1].close, sum(c.volume for c in chunk)))
        return out

    def _coarse(self, tf: Timeframe) -> list[Candle]:
        t0, step, count = timeline(tf, self.n, self.start)
        hours = step // HOUR
        labels = [t0 + k * step for k in range(count)]
        full = [ts for ts in labels if ts - step + HOUR >= self.start]  # every hour of the bar is in the H1 span
        n_pre = count - len(full)
        out: list[Candle] = []
        if n_pre:
            anchor_ts = labels[n_pre - 1]
            anchor = self.price_at(anchor_ts) if anchor_ts >= self.start else self.h1[0].open
            rng = random.Random(f"{self.seed}:{self.symbol}:{self.regime.value}:{self.n}:{tf.value}:pre")
            drift, noise = _SHAPES[self.regime]
            rets = [drift + rng.gauss(0, noise) for _ in range(n_pre)]
            closes = [anchor] * n_pre
            for j in range(n_pre - 2, -1, -1):
                closes[j] = max(0.01, closes[j + 1] / (1 + rets[j + 1]))
            base_vol = 5000.0 * (3.0 if self.regime == Regime.HIGH_VOLATILITY else 1.0) * hours
            for j in range(n_pre):
                o = closes[j - 1] if j else closes[0] / (1 + rets[0])
                wick = abs(rng.gauss(0, noise * 0.6)) + 0.0005
                out.append(_c(labels[j], o, max(o, closes[j]) * (1 + wick), min(o, closes[j]) * (1 - wick),
                              closes[j], base_vol * (0.5 + rng.random())))
        for ts in full:
            first = (ts - step + HOUR - self.start) // HOUR
            chunk = self.h1[first: first + hours]
            o = out[-1].close if out else chunk[0].open
            out.append(_c(ts, o, max(c.high for c in chunk), min(c.low for c in chunk), chunk[-1].close,
                          sum(c.volume for c in chunk)))
        return out

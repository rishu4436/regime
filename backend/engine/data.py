"""Candle sanitising + timeframe helpers for real on-chain tokenized-stock data.

Real Binance Web3 ``market/candles`` data (Bot 4, ``[o,h,l,c,v,ts_ms,tradeCount]``) differs from the seeded mocks:
hourly bars are SPARSE off-hours (weekends / US market closed), DEX volume is often 0, rows can be malformed,
duplicated (the in-progress bar is re-sent) or unordered, and prices can be tiny. Every engine entry point runs
``sanitize_candles`` first, so indicators never see bad input.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from backend.contracts import Candle, Timeframe, TokenType

TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14_400,
    Timeframe.D1: 86_400,
}
DEFAULT_BAR_SECONDS = 3600
GAP_FACTOR = 1.5  # consecutive bars further apart than 1.5 bars = a gap (weekend, market closed, missing bars)


@dataclass
class DataReport:
    """What sanitize_candles did. ``issues`` are human-readable and safe to put in a Signal reason."""

    received: int = 0
    kept: int = 0
    dropped_invalid: int = 0  # None / non-finite / <= 0 prices / inconsistent OHLC / no timestamp
    duplicates: int = 0  # same timestamp seen twice (last one kept)
    unsorted: bool = False
    volume_missing: int = 0  # None / NaN / negative volume replaced by 0
    zero_volume: int = 0  # bars with volume == 0 after cleaning
    gaps: int = 0  # gaps > GAP_FACTOR bars between consecutive kept bars
    bar_seconds: int = DEFAULT_BAR_SECONDS
    issues: list[str] = field(default_factory=list)


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _num(x: Any) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ts(x: Any) -> Optional[datetime]:
    if isinstance(x, datetime):
        return x.replace(tzinfo=timezone.utc) if x.tzinfo is None else x.astimezone(timezone.utc)
    return None


def sanitize_candles(candles: Optional[Sequence[Any]]) -> tuple[list[Candle], DataReport]:
    """Return valid candles sorted oldest-first with unique timestamps, plus a report.

    - drops entries that are None, have no/naive-invalid timestamp, non-finite or <= 0 prices, or high < low
    - widens high/low to contain open/close (same repair Bot 4's client does), so slightly inconsistent rows survive
    - volume None / NaN / negative -> 0.0 (counted as missing; volume filters then stay out)
    - duplicate timestamps: the LAST occurrence wins (exchanges re-send the in-progress bar)
    Accepts Candle objects, ``Candle.model_construct`` objects (unvalidated) and plain dicts with the same keys.
    """
    rep = DataReport(received=len(candles or []))
    by_ts: dict[datetime, Candle] = {}
    prev_ts: Optional[datetime] = None
    for raw in candles or []:
        if raw is None:
            rep.dropped_invalid += 1
            continue
        ts = _ts(_get(raw, "timestamp"))
        o, h, lo, c = (_num(_get(raw, k)) for k in ("open", "high", "low", "close"))
        if ts is None or None in (o, h, lo, c) or min(o, h, lo, c) <= 0 or h < lo:  # type: ignore[operator]
            rep.dropped_invalid += 1
            continue
        v = _num(_get(raw, "volume"))
        if v is None or v < 0:
            rep.volume_missing += 1
            v = 0.0
        if prev_ts is not None and ts < prev_ts:
            rep.unsorted = True
        prev_ts = ts
        if ts in by_ts:
            rep.duplicates += 1
        h2, l2 = max(h, o, c), min(lo, o, c)  # type: ignore[type-var]
        if isinstance(raw, Candle) and (h2, l2, v, ts) == (raw.high, raw.low, raw.volume, raw.timestamp):
            by_ts[ts] = raw
        else:
            by_ts[ts] = Candle(timestamp=ts, open=o, high=h2, low=l2, close=c, volume=v)
    out = [by_ts[t] for t in sorted(by_ts)]
    rep.kept = len(out)
    rep.zero_volume = sum(1 for k in out if k.volume == 0)
    rep.bar_seconds = infer_bar_seconds(out)
    limit = GAP_FACTOR * rep.bar_seconds
    rep.gaps = sum(1 for a, b in zip(out, out[1:], strict=False) if (b.timestamp - a.timestamp).total_seconds() > limit)
    if rep.dropped_invalid:
        rep.issues.append(f"dropped {rep.dropped_invalid} invalid candle(s)")
    if rep.duplicates:
        rep.issues.append(f"merged {rep.duplicates} duplicate timestamp(s)")
    if rep.unsorted:
        rep.issues.append("candles were not in time order (sorted)")
    if rep.volume_missing:
        rep.issues.append(f"{rep.volume_missing} candle(s) without volume (treated as 0)")
    return out, rep


def infer_bar_seconds(candles: Sequence[Candle]) -> int:
    """Bar length from the data: the median of the smallest positive spacings (robust to weekend/off-hours gaps),
    snapped to the nearest contract Timeframe. DEFAULT_BAR_SECONDS when it cannot be inferred."""
    spacings = ((b.timestamp - a.timestamp).total_seconds() for a, b in zip(candles, candles[1:], strict=False))
    diffs = sorted(d for d in spacings if d > 0)
    if not diffs:
        return DEFAULT_BAR_SECONDS
    core = diffs[: max(1, len(diffs) // 2)]  # lower half: gaps only ever make spacings LONGER
    med = statistics.median(core)
    return min(TIMEFRAME_SECONDS.values(), key=lambda s: abs(math.log(s / med)))


def bar_seconds_for(timeframe: Optional[Timeframe], candles: Sequence[Candle] = ()) -> int:
    if timeframe is not None:
        return TIMEFRAME_SECONDS[Timeframe(timeframe)]
    return infer_bar_seconds(candles)


def is_dead_bar(c: Candle) -> bool:
    """A placeholder bar from a closed / illiquid market: no range and no volume."""
    return c.high == c.low and c.volume == 0


def contiguous_pairs(candles: Sequence[Candle], bar_seconds: int) -> list[tuple[int, int]]:
    """Index pairs (i-1, i) not separated by a gap (used to keep weekend jumps out of volatility estimates)."""
    lim = GAP_FACTOR * bar_seconds
    return [
        (i - 1, i)
        for i in range(1, len(candles))
        if (candles[i].timestamp - candles[i - 1].timestamp).total_seconds() <= lim
    ]


def infer_token_type(symbol: str) -> TokenType:
    """Same suffix convention as Bot 4 (``backend/exchange/tokens.py``): Ondo '...on', xStock '...x', bStock '...B'."""
    if symbol.endswith("on"):
        return TokenType.ONDO
    if symbol.endswith("x"):
        return TokenType.XSTOCK
    if symbol.endswith("B"):
        return TokenType.BSTOCK
    return TokenType.OTHER


STOCK_TOKEN_TYPES = {TokenType.ONDO, TokenType.XSTOCK, TokenType.BSTOCK}


def staleness(last_ts: datetime, now: datetime, bar_seconds: int) -> float:
    """Age of the last candle's OPEN time in bars (negative = from the future)."""
    return (now - _ts(last_ts)).total_seconds() / bar_seconds  # type: ignore[operator]


def as_utc(ts: datetime) -> datetime:
    return _ts(ts)  # type: ignore[return-value]


__all__ = [
    "DataReport",
    "TIMEFRAME_SECONDS",
    "sanitize_candles",
    "infer_bar_seconds",
    "bar_seconds_for",
    "is_dead_bar",
    "contiguous_pairs",
    "infer_token_type",
    "STOCK_TOKEN_TYPES",
    "staleness",
    "as_utc",
]

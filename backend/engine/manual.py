"""Manual-strategy rule evaluation shared by evaluate_manual (last bar) and backtest_manual (every bar)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from backend.contracts import (
    BreakoutDirection,
    Candle,
    ConditionLogic,
    ExitReason,
    ManualStrategy,
    MarketHoursFilter,
)

from . import indicators as ind
from .indicators import Series

DEFAULT_VOLUME_PERIOD = 20  # used when entry.volume_above_avg.period is None (contract default) -> no look-ahead


@dataclass
class ManualIndicators:
    """All indicator series a ManualStrategy needs, computed once over the candle list (all causal)."""

    rsi_entry: Series
    rsi_exit: Series
    sma: Series
    ema: Series
    macd_entry: tuple[Series, Series]
    macd_exit: tuple[Series, Series]
    bb_lower: Series
    vol_avg: Series
    vol_active: Series  # fraction of bars with volume > 0 in the volume-average window
    breakout_high: Series
    breakout_low: Series

    @classmethod
    def compute(cls, s: ManualStrategy, candles: Sequence[Candle]) -> "ManualIndicators":
        closes = [c.close for c in candles]
        e, x = s.entry, s.exit
        me = ind.macd(
            closes,
            e.macd_bullish_cross.params.fast,
            e.macd_bullish_cross.params.slow,
            e.macd_bullish_cross.params.signal,
        )
        mx = ind.macd(
            closes,
            x.macd_bearish_cross.params.fast,
            x.macd_bearish_cross.params.slow,
            x.macd_bearish_cross.params.signal,
        )
        bb_lower, _, _ = ind.bollinger(closes, e.bollinger_lower.period, e.bollinger_lower.std_dev)
        vol_period = e.volume_above_avg.period if e.volume_above_avg.period is not None else DEFAULT_VOLUME_PERIOD
        return cls(
            rsi_entry=ind.rsi(closes, e.rsi_below.period),
            rsi_exit=ind.rsi(closes, x.rsi_above.period),
            sma=ind.sma(closes, e.price_above_sma.period),
            ema=ind.ema(closes, e.price_above_ema.period),
            macd_entry=(me[0], me[1]),
            macd_exit=(mx[0], mx[1]),
            bb_lower=bb_lower,
            vol_avg=ind.volume_average([c.volume for c in candles], vol_period),
            vol_active=ind.sma([1.0 if c.volume > 0 else 0.0 for c in candles], vol_period),
            breakout_high=ind.rolling_max([c.high for c in candles], e.breakout.lookback),
            breakout_low=ind.rolling_min([c.low for c in candles], e.breakout.lookback),
        )


@dataclass
class EntryEvaluation:
    ok: bool
    met: list[str]
    failed: list[str]
    skipped: list[str]  # enabled but still in warm-up
    detail: list[str]


def evaluate_entry(s: ManualStrategy, mi: ManualIndicators, candles: Sequence[Candle], i: int) -> EntryEvaluation:
    """Each ENABLED condition whose indicator is available contributes one boolean; warm-up ones are skipped.
    No booleans -> no entry. AND = all true, OR = any true. Names = EntryConditions field names."""
    c = candles[i]
    e = s.entry
    results: dict[str, Optional[bool]] = {}
    detail: list[str] = []

    if e.rsi_below.enabled:
        v = mi.rsi_entry[i]
        results["rsi_below"] = None if v is None else v < e.rsi_below.threshold
        if v is not None:
            detail.append(
                f"RSI({e.rsi_below.period})={v:.1f} "
                f"{'<' if v < e.rsi_below.threshold else '>='} {e.rsi_below.threshold:g}"
            )
    if e.price_above_sma.enabled:
        v = mi.sma[i]
        results["price_above_sma"] = None if v is None else c.close > v
        if v is not None:
            detail.append(f"close {c.close:.4g} {'>' if c.close > v else '<='} SMA({e.price_above_sma.period}) {v:.4g}")
    if e.price_above_ema.enabled:
        v = mi.ema[i]
        results["price_above_ema"] = None if v is None else c.close > v
        if v is not None:
            detail.append(f"close {c.close:.4g} {'>' if c.close > v else '<='} EMA({e.price_above_ema.period}) {v:.4g}")
    if e.macd_bullish_cross.enabled:
        r = ind.crossed_above(mi.macd_entry[0], mi.macd_entry[1], i)
        results["macd_bullish_cross"] = r
        if r is not None:
            detail.append("MACD crossed above signal" if r else "no MACD bullish cross")
    if e.bollinger_lower.enabled:
        v = mi.bb_lower[i]
        if v is None:
            results["bollinger_lower"] = None
        else:
            lim = v * (1 + e.bollinger_lower.proximity_pct / 100)
            results["bollinger_lower"] = c.close < lim
            detail.append(
                f"close {c.close:.4g} {'<' if c.close < lim else '>='} "
                f"lower BB*(1+{e.bollinger_lower.proximity_pct:g}%) {lim:.4g}"
            )
    if e.volume_above_avg.enabled:
        v = mi.vol_avg[i]
        if v is None:
            results["volume_above_avg"] = None
        elif not _has_volume_baseline(mi, i, c):  # sparse on-chain volume is never a reason to buy
            results["volume_above_avg"] = False
            detail.append(f"no volume data (volume {c.volume:.4g}, avg {v:.4g}, {_active_pct(mi, i)} bars traded)")
        else:
            results["volume_above_avg"] = c.volume > e.volume_above_avg.multiplier * v
            detail.append(f"volume {c.volume:.4g} vs {e.volume_above_avg.multiplier:g}x avg {v:.4g}")
    if e.breakout.enabled:
        if e.breakout.direction == BreakoutDirection.HIGH:
            v = mi.breakout_high[i]
            results["breakout"] = None if v is None else c.close > v
            if v is not None:
                detail.append(f"close {c.close:.4g} vs {e.breakout.lookback}-bar high {v:.4g}")
        else:
            v = mi.breakout_low[i]
            results["breakout"] = None if v is None else c.close < v
            if v is not None:
                detail.append(f"close {c.close:.4g} vs {e.breakout.lookback}-bar low {v:.4g}")

    met = [k for k, r in results.items() if r is True]
    failed = [k for k, r in results.items() if r is False]
    skipped = [k for k, r in results.items() if r is None]
    bools = met + failed
    if not bools:
        ok = False
    elif e.logic == ConditionLogic.AND:
        ok = not failed
    else:
        ok = bool(met)
    return EntryEvaluation(ok=ok, met=met, failed=failed, skipped=skipped, detail=detail)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def in_market_hours(ts: datetime, f: MarketHoursFilter) -> bool:
    """[start, end) in UTC on the candle timestamp; weekdays Mon-Fri if weekdays_only. start > end wraps midnight."""
    t = _utc(ts)
    if f.weekdays_only and t.weekday() >= 5:
        return False
    h = t.hour
    if f.start_hour_utc == f.end_hour_utc:
        return True  # degenerate window = whole day
    if f.start_hour_utc < f.end_hour_utc:
        return f.start_hour_utc <= h < f.end_hour_utc
    return h >= f.start_hour_utc or h < f.end_hour_utc


def check_filters(s: ManualStrategy, mi: ManualIndicators, candles: Sequence[Candle], i: int) -> tuple[bool, list[str]]:
    """Hard filters (always ANDed with the entry result). Returns (passed, human-readable failures)."""
    c = candles[i]
    fails: list[str] = []
    mh = s.filters.market_hours
    if mh.enabled and not in_market_hours(c.timestamp, mh):
        fails.append(
            f"outside market hours ({mh.start_hour_utc:02d}:00-{mh.end_hour_utc:02d}:00 UTC"
            f"{', weekdays' if mh.weekdays_only else ''})"
        )
    mv = s.filters.min_volume
    if mv.enabled:
        avg = mi.vol_avg[i]
        if avg is None:
            fails.append("min-volume filter: volume average still in warm-up")
        elif not _has_volume_baseline(mi, i, c):
            fails.append(
                f"min-volume filter: no volume data (volume {c.volume:.4g}, avg {avg:.4g}, "
                f"{_active_pct(mi, i)} bars traded)"
            )
        elif not c.volume > mv.multiplier * avg:
            fails.append(f"min-volume filter: volume {c.volume:.4g} <= {mv.multiplier:g}x avg {avg:.4g}")
        if mv.min_volume is not None and c.volume < mv.min_volume:
            fails.append(f"min-volume filter: volume {c.volume:.4g} < minimum {mv.min_volume:g}")
    return (not fails), fails


MIN_ACTIVE_VOLUME_FRACTION = 0.5


def _has_volume_baseline(mi: ManualIndicators, i: int, c: Candle) -> bool:
    """Volume comparisons need a real baseline: this bar traded, the average is positive and at least half of
    the bars in the window traded (otherwise one stray on-chain trade looks like a huge "spike")."""
    avg, act = mi.vol_avg[i], mi.vol_active[i]
    return avg is not None and avg > 0 and c.volume > 0 and (act is not None and act >= MIN_ACTIVE_VOLUME_FRACTION)


def _active_pct(mi: ManualIndicators, i: int) -> str:
    act = mi.vol_active[i]
    return "n/a" if act is None else f"{act * 100:.0f}%"


def indicator_exit(s: ManualStrategy, mi: ManualIndicators, i: int) -> Optional[tuple[ExitReason, str]]:
    """RSI exit then MACD bearish-cross exit, on the bar close."""
    x = s.exit
    if x.rsi_above.enabled:
        v = mi.rsi_exit[i]
        if v is not None and v > x.rsi_above.threshold:
            return ExitReason.RSI_EXIT, f"RSI({x.rsi_above.period})={v:.1f} > {x.rsi_above.threshold:g}"
    if x.macd_bearish_cross.enabled and ind.crossed_below(mi.macd_exit[0], mi.macd_exit[1], i):
        return ExitReason.MACD_EXIT, "MACD crossed below signal"
    return None

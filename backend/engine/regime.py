"""Rule-based regime detector (6 regimes from backend.contracts.Regime). Pure function of candles.

Real-data handling (see README "Live / mainnet"):
- candles are sanitised first (``data.sanitize_candles``)
- "dead" placeholder bars (no range, no volume = closed / illiquid market) are ignored; if most recent bars are dead
  the market is treated as closed -> SIDEWAYS with low confidence (never a trend)
- volatility only uses returns between CONTIGUOUS bars, so a weekend / overnight gap is not mistaken for volatility
- all percent thresholds are calibrated for 1h bars and scaled by sqrt(bar_hours) for other timeframes
"""

from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

from backend.contracts import Candle, Regime, RegimeAnalysis, Timeframe

from . import indicators as ind
from .data import DEFAULT_BAR_SECONDS, bar_seconds_for, contiguous_pairs, is_dead_bar, sanitize_candles

# Thresholds (percent units) calibrated on backend.mocks.generate_candles (1h) for all six shapes and many seeds.
SMA_PERIOD = 50
EMA_PERIOD = 20
RSI_PERIOD = 14
SLOPE_LOOKBACK = 10  # bars over which the SMA50 slope / return are measured
VOL_LOOKBACK = 20
MIN_BARS = SMA_PERIOD + SLOPE_LOOKBACK + 1  # 61 ACTIVE bars

HIGH_VOL_STDEV_PCT = 2.0  # stdev of per-bar log returns (last 20 bars) >= 2% -> HIGH_VOLATILITY
PULLBACK_RSI = 30.0  # RSI(14) below this ...  (RSI is unit-free: never scaled)
PULLBACK_RET_PCT = -8.0  # ... and a 10-bar return <= -8% (sharp drop) -> OVERSOLD_PULLBACK
DOWNTREND_SLOPE_PCT = -1.0  # close < SMA50 and SMA50 10-bar slope <= -1% -> DOWNTREND
STRONG_SLOPE_PCT = 2.9  # close > EMA20 > SMA50, slope >= 2.9% and close >= 7% above SMA50 -> STRONG_UPTREND
STRONG_DIST_PCT = 7.0
MILD_SLOPE_PCT = 0.75  # close > SMA50 and slope >= 0.75% -> MILD_UPTREND; anything else -> SIDEWAYS
DEAD_FRACTION_CLOSED = 0.5  # > 50% dead bars among the last VOL_LOOKBACK -> market closed / illiquid
MIN_CONTIGUOUS_RETURNS = 5


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def timeframe_scale(bar_seconds: int) -> float:
    """sqrt(bar_hours), clamped to [0.25, 5]: random-walk moves grow with the square root of time, so the 1h-tuned
    percent thresholds are multiplied by this (1m -> 0.25, 5m -> 0.29, 15m -> 0.5, 4h -> 2, 1d -> 4.9)."""
    return _clamp(math.sqrt(bar_seconds / 3600), 0.25, 5.0)


def compute_features(candles: Sequence[Candle], bar_seconds: int = DEFAULT_BAR_SECONDS) -> dict[str, float]:
    """Features of the LAST bar. ``candles`` must be sanitised, active and at least MIN_BARS long."""
    closes = [c.close for c in candles]
    s50 = ind.sma(closes, SMA_PERIOD)
    e20 = ind.ema(closes, EMA_PERIOD)
    r = ind.rsi(closes, RSI_PERIOD)
    a = ind.atr([c.high for c in candles], [c.low for c in candles], closes, 14)
    last = closes[-1]
    window_start = len(candles) - VOL_LOOKBACK - 1
    pairs = [(i, j) for i, j in contiguous_pairs(candles, bar_seconds) if i >= window_start]
    used_gap_returns = 0.0
    if len(pairs) < MIN_CONTIGUOUS_RETURNS:  # very sparse data: fall back to all consecutive pairs
        pairs = [(i - 1, i) for i in range(window_start + 1, len(candles))]
        used_gap_returns = 1.0
    rets = [math.log(closes[j] / closes[i]) for i, j in pairs]
    hi20 = max(c.high for c in candles[-VOL_LOOKBACK:])
    sma_now, sma_prev = s50[-1], s50[-1 - SLOPE_LOOKBACK]
    assert sma_now is not None and sma_prev is not None and e20[-1] is not None and r[-1] is not None
    return {
        "close": last,
        "sma50": sma_now,
        "ema20": e20[-1],
        "rsi": r[-1],
        "atr_pct": (a[-1] or 0.0) / last * 100,
        "volatility_pct": statistics.pstdev(rets) * 100 if len(rets) > 1 else 0.0,
        "sma_slope_pct": (sma_now / sma_prev - 1) * 100,
        "return_10_pct": (last / closes[-1 - SLOPE_LOOKBACK] - 1) * 100,
        "drawdown_from_high_pct": (last / hi20 - 1) * 100,
        "dist_from_sma_pct": (last / sma_now - 1) * 100,
        "dist_from_ema_pct": (last / e20[-1] - 1) * 100,
        "vol_returns_used": float(len(rets)),
        "vol_used_gap_returns": used_gap_returns,
    }


def _insufficient(reason: str, ts, features: dict[str, float], conf: float = 0.1) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=Regime.SIDEWAYS,
        confidence=conf,
        timestamp=ts,
        reason=reason,
        features={**features, "insufficient_data": 1.0},
    )


def detect_regime(candles: Sequence[Candle], timeframe: Optional[Timeframe] = None) -> RegimeAnalysis:
    """Classify the LAST bar. Rule order (first match wins): HIGH_VOLATILITY -> OVERSOLD_PULLBACK -> DOWNTREND
    -> STRONG_UPTREND -> MILD_UPTREND -> SIDEWAYS. Not enough usable data -> SIDEWAYS, low confidence, and
    ``features["insufficient_data"] == 1`` (the engine stays out on that). ``timeframe`` None = inferred."""
    clean, rep = sanitize_candles(candles)
    if not clean:
        return RegimeAnalysis(
            regime=Regime.SIDEWAYS,
            confidence=0.0,
            reason="No valid candles: defaulting to SIDEWAYS",
            features={"bars": 0.0, "insufficient_data": 1.0},
        )
    ts = clean[-1].timestamp
    bar_s = bar_seconds_for(timeframe, clean)
    scale = timeframe_scale(bar_s)
    base = {"bars": float(len(clean)), "bar_seconds": float(bar_s), "threshold_scale": round(scale, 6)}
    recent = clean[-VOL_LOOKBACK:]
    dead_frac = sum(map(is_dead_bar, recent)) / len(recent)
    base["dead_bar_fraction"] = round(dead_frac, 6)
    if len(recent) >= 5 and dead_frac > DEAD_FRACTION_CLOSED:
        return _insufficient(
            f"{dead_frac:.0%} of the last {len(recent)} bars have no price range and no volume "
            "(market closed / illiquid): SIDEWAYS, no trend signal",
            ts,
            base,
            conf=0.2,
        )
    active = [c for c in clean if not is_dead_bar(c)]
    base["active_bars"] = float(len(active))
    if len(active) < MIN_BARS:
        return _insufficient(
            f"Only {len(active)} usable candles (< {MIN_BARS} needed): defaulting to SIDEWAYS with low confidence",
            ts,
            base,
        )
    f = {**compute_features(active, bar_s), **base}
    vol, slope, rsi_v, ret10 = f["volatility_pct"], f["sma_slope_pct"], f["rsi"], f["return_10_pct"]
    close, sma50, ema20, dist = f["close"], f["sma50"], f["ema20"], f["dist_from_sma_pct"]
    hv, pb, dt = HIGH_VOL_STDEV_PCT * scale, PULLBACK_RET_PCT * scale, DOWNTREND_SLOPE_PCT * scale
    st, sd, mi = STRONG_SLOPE_PCT * scale, STRONG_DIST_PCT * scale, MILD_SLOPE_PCT * scale

    if vol >= hv:
        regime, conf = Regime.HIGH_VOLATILITY, 0.6 + 0.4 * _clamp(vol / hv - 1)
        reason = f"Per-bar volatility {vol:.2f}% >= {hv:.2f}% (large swings)"
    elif rsi_v < PULLBACK_RSI and ret10 <= pb:
        regime, conf = Regime.OVERSOLD_PULLBACK, 0.6 + 0.4 * _clamp((PULLBACK_RSI - rsi_v) / PULLBACK_RSI)
        reason = f"Sharp drop ({ret10:.1f}% over {SLOPE_LOOKBACK} bars) with RSI {rsi_v:.1f} < {PULLBACK_RSI:g}"
    elif close < sma50 and slope <= dt:
        regime, conf = Regime.DOWNTREND, 0.6 + 0.4 * _clamp(-slope / (4 * scale))
        reason = f"Price {dist:.1f}% below a falling SMA{SMA_PERIOD} (slope {slope:.2f}%)"
    elif close > ema20 > sma50 and slope >= st and dist >= sd:
        regime, conf = Regime.STRONG_UPTREND, 0.6 + 0.4 * _clamp(slope / st - 1)
        reason = f"Price {dist:.1f}% above a rising SMA{SMA_PERIOD} (slope {slope:.2f}%), EMA{EMA_PERIOD} above SMA"
    elif close > sma50 and slope >= mi:
        regime, conf = Regime.MILD_UPTREND, 0.55 + 0.35 * _clamp((slope - mi) / (2 * scale))
        reason = f"Price {dist:.1f}% above SMA{SMA_PERIOD} with a moderate slope ({slope:.2f}%)"
    else:
        regime, conf = Regime.SIDEWAYS, 0.5 + 0.4 * _clamp(1 - abs(slope) / mi)
        reason = f"Flat SMA{SMA_PERIOD} (slope {slope:.2f}%) and low volatility ({vol:.2f}%)"
    if rep.issues:
        reason += " [data: " + "; ".join(rep.issues) + "]"
    price_keys = {"close", "sma50", "ema20"}  # keep full precision for tiny-priced tokens
    return RegimeAnalysis(
        regime=regime,
        confidence=round(conf, 3),
        reason=reason,
        features={k: (v if k in price_keys else round(v, 6)) for k, v in f.items()},
        timestamp=ts,
    )

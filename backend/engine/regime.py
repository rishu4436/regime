"""Rule-based regime detector (6 regimes from backend.contracts.Regime). Pure function of candles."""

from __future__ import annotations

import math
import statistics
from typing import Sequence

from backend.contracts import Candle, Regime, RegimeAnalysis

from . import indicators as ind

# Thresholds (percent units). Calibrated on backend.mocks.generate_candles for all six shapes and several seeds;
# they are per-BAR quantities, so they are most meaningful on 1h candles (the default timeframe).
SMA_PERIOD = 50
EMA_PERIOD = 20
RSI_PERIOD = 14
SLOPE_LOOKBACK = 10  # bars over which the SMA50 slope / return are measured
VOL_LOOKBACK = 20
MIN_BARS = SMA_PERIOD + SLOPE_LOOKBACK + 1  # 61

HIGH_VOL_STDEV_PCT = 2.0  # stdev of per-bar log returns (last 20 bars) >= 2% -> HIGH_VOLATILITY
PULLBACK_RSI = 30.0  # RSI(14) below this ...
PULLBACK_RET_PCT = -8.0  # ... and a 10-bar return <= -8% (sharp drop) -> OVERSOLD_PULLBACK
DOWNTREND_SLOPE_PCT = -1.0  # close < SMA50 and SMA50 10-bar slope <= -1% -> DOWNTREND
STRONG_SLOPE_PCT = 2.9  # close > EMA20 > SMA50, slope >= 2.9% and close >= 7% above SMA50 -> STRONG_UPTREND
STRONG_DIST_PCT = 7.0
MILD_SLOPE_PCT = 0.75  # close > SMA50 and slope >= 0.75% -> MILD_UPTREND; anything else -> SIDEWAYS


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_features(candles: Sequence[Candle]) -> dict[str, float]:
    closes = [c.close for c in candles]
    s50 = ind.sma(closes, SMA_PERIOD)
    e20 = ind.ema(closes, EMA_PERIOD)
    r = ind.rsi(closes, RSI_PERIOD)
    a = ind.atr([c.high for c in candles], [c.low for c in candles], closes, 14)
    last = closes[-1]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - VOL_LOOKBACK, len(closes))]
    hi20 = max(c.high for c in candles[-VOL_LOOKBACK:])
    sma_now, sma_prev = s50[-1], s50[-1 - SLOPE_LOOKBACK]
    assert sma_now is not None and sma_prev is not None and e20[-1] is not None and r[-1] is not None
    return {
        "close": last,
        "sma50": sma_now,
        "ema20": e20[-1],
        "rsi": r[-1],
        "atr_pct": (a[-1] or 0.0) / last * 100,
        "volatility_pct": statistics.pstdev(rets) * 100,
        "sma_slope_pct": (sma_now / sma_prev - 1) * 100,
        "return_10_pct": (last / closes[-1 - SLOPE_LOOKBACK] - 1) * 100,
        "drawdown_from_high_pct": (last / hi20 - 1) * 100,
        "dist_from_sma_pct": (last / sma_now - 1) * 100,
        "dist_from_ema_pct": (last / e20[-1] - 1) * 100,
    }


def detect_regime(candles: Sequence[Candle]) -> RegimeAnalysis:
    """Classify the LAST bar. Rule order (first match wins): HIGH_VOLATILITY -> OVERSOLD_PULLBACK -> DOWNTREND
    -> STRONG_UPTREND -> MILD_UPTREND -> SIDEWAYS. Fewer than MIN_BARS candles -> SIDEWAYS with confidence 0.1."""
    if not candles:
        return RegimeAnalysis(
            regime=Regime.SIDEWAYS, confidence=0.0, reason="No candles: defaulting to SIDEWAYS", features={"bars": 0.0}
        )
    ts = candles[-1].timestamp
    if len(candles) < MIN_BARS:
        return RegimeAnalysis(
            regime=Regime.SIDEWAYS,
            confidence=0.1,
            timestamp=ts,
            features={"bars": float(len(candles))},
            reason=f"Only {len(candles)} candles (< {MIN_BARS} needed): defaulting to SIDEWAYS with low confidence",
        )
    f = compute_features(candles)
    f["bars"] = float(len(candles))
    vol, slope, rsi_v, ret10 = f["volatility_pct"], f["sma_slope_pct"], f["rsi"], f["return_10_pct"]
    close, sma50, ema20, dist = f["close"], f["sma50"], f["ema20"], f["dist_from_sma_pct"]

    if vol >= HIGH_VOL_STDEV_PCT:
        regime, conf = Regime.HIGH_VOLATILITY, 0.6 + 0.4 * _clamp(vol / HIGH_VOL_STDEV_PCT - 1)
        reason = f"Per-bar volatility {vol:.2f}% >= {HIGH_VOL_STDEV_PCT}% (large swings)"
    elif rsi_v < PULLBACK_RSI and ret10 <= PULLBACK_RET_PCT:
        regime, conf = Regime.OVERSOLD_PULLBACK, 0.6 + 0.4 * _clamp((PULLBACK_RSI - rsi_v) / PULLBACK_RSI)
        reason = f"Sharp drop ({ret10:.1f}% over {SLOPE_LOOKBACK} bars) with RSI {rsi_v:.1f} < {PULLBACK_RSI:g}"
    elif close < sma50 and slope <= DOWNTREND_SLOPE_PCT:
        regime, conf = Regime.DOWNTREND, 0.6 + 0.4 * _clamp(-slope / 4)
        reason = f"Price {dist:.1f}% below a falling SMA{SMA_PERIOD} (slope {slope:.2f}%)"
    elif close > ema20 > sma50 and slope >= STRONG_SLOPE_PCT and dist >= STRONG_DIST_PCT:
        regime, conf = Regime.STRONG_UPTREND, 0.6 + 0.4 * _clamp(slope / STRONG_SLOPE_PCT - 1)
        reason = f"Price {dist:.1f}% above a rising SMA{SMA_PERIOD} (slope {slope:.2f}%), EMA{EMA_PERIOD} above SMA"
    elif close > sma50 and slope >= MILD_SLOPE_PCT:
        regime, conf = Regime.MILD_UPTREND, 0.55 + 0.35 * _clamp((slope - MILD_SLOPE_PCT) / 2)
        reason = f"Price {dist:.1f}% above SMA{SMA_PERIOD} with a moderate slope ({slope:.2f}%)"
    else:
        regime, conf = Regime.SIDEWAYS, 0.5 + 0.4 * _clamp(1 - abs(slope) / MILD_SLOPE_PCT)
        reason = f"Flat SMA{SMA_PERIOD} (slope {slope:.2f}%) and low volatility ({vol:.2f}%)"
    return RegimeAnalysis(
        regime=regime,
        confidence=round(conf, 3),
        reason=reason,
        features={k: round(v, 6) for k, v in f.items()},
        timestamp=ts,
    )

"""Auto Mode: (regime, trader style) -> exactly ONE preset, plus the five preset evaluators.

Each preset is a small, deterministic rule on the latest bar. Presets only propose BUY / SELL / STAY_OUT;
sizing caps, exposure and price stops (SL/TP/trailing) are the risk layer's job (Bot 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Optional, Sequence

from backend.contracts import Action, Candle, ExitReason, PresetName, Regime, TraderStyle

from . import indicators as ind

P, D = PresetName, Regime

# Ordered preference per cell: the first ENABLED preset wins; an empty list (or none enabled) = stay out.
# Size multiplier scales the style's base position size (e.g. 0.5 in high volatility).
PRESET_MAP: dict[Regime, dict[TraderStyle, tuple[list[PresetName], float]]] = {
    D.STRONG_UPTREND: {
        TraderStyle.PATIENT: ([P.SMART_DCA, P.DCA, P.TREND_FOLLOWING], 1.0),
        TraderStyle.BALANCED: ([P.TREND_FOLLOWING, P.SMART_DCA, P.DCA], 1.0),
        TraderStyle.ACTIVE: ([P.TREND_FOLLOWING, P.SMART_DCA], 1.0),
    },
    D.MILD_UPTREND: {
        TraderStyle.PATIENT: ([P.SMART_DCA, P.DCA], 1.0),
        TraderStyle.BALANCED: ([P.SMART_DCA, P.TREND_FOLLOWING, P.DCA], 1.0),
        TraderStyle.ACTIVE: ([P.TREND_FOLLOWING, P.SMART_DCA], 1.0),
    },
    D.SIDEWAYS: {
        TraderStyle.PATIENT: ([P.DCA, P.SMART_DCA, P.GRID], 1.0),
        TraderStyle.BALANCED: ([P.GRID, P.SMART_DCA, P.DCA], 1.0),
        TraderStyle.ACTIVE: ([P.GRID, P.BUY_THE_DIP], 1.0),
    },
    D.OVERSOLD_PULLBACK: {
        TraderStyle.PATIENT: ([P.SMART_DCA, P.BUY_THE_DIP, P.DCA], 1.0),
        TraderStyle.BALANCED: ([P.BUY_THE_DIP, P.SMART_DCA], 1.0),
        TraderStyle.ACTIVE: ([P.BUY_THE_DIP], 1.0),
    },
    D.HIGH_VOLATILITY: {
        TraderStyle.PATIENT: ([P.DCA, P.SMART_DCA], 0.5),
        TraderStyle.BALANCED: ([], 0.0),  # pause
        TraderStyle.ACTIVE: ([P.GRID], 0.5),
    },
    D.DOWNTREND: {
        TraderStyle.PATIENT: ([P.DCA], 0.25),  # minimal DCA only
        TraderStyle.BALANCED: ([], 0.0),
        TraderStyle.ACTIVE: ([], 0.0),
    },
}

# Base position size (% of AutoConfig.capital) per style, before the regime size multiplier.
BASE_SIZE_PCT: dict[TraderStyle, float] = {
    TraderStyle.PATIENT: 5.0,
    TraderStyle.BALANCED: 10.0,
    TraderStyle.ACTIVE: 15.0,
}


@dataclass(frozen=True)
class StyleParams:
    dca_interval_bars: int  # DCA / Smart DCA buy every N bars (aligned to the candle clock)
    smart_dca_rsi_max: float  # Smart DCA buys only when RSI(14) below this ...
    smart_dca_max_above_sma_pct: float  # ... or close is at most this % above SMA(20)
    dip_rsi: float  # Buy the Dip: RSI(14) below this
    dip_drop_pct: float  # ... and close at least this % below the 20-bar high
    dip_exit_rsi: float  # Buy the Dip exit: RSI recovered above this
    grid_lookback: int  # Grid range = min low / max high of the last N bars
    grid_levels: int  # number of grid cells in the range
    trend_fast: int = 20  # Trend: EMA fast
    trend_slow: int = 50  # Trend: SMA slow


STYLE_PARAMS: dict[TraderStyle, StyleParams] = {
    TraderStyle.PATIENT: StyleParams(24, 45, 0.0, 30, 8.0, 55, 50, 4),
    TraderStyle.BALANCED: StyleParams(12, 50, 1.0, 32, 6.0, 55, 40, 5),
    TraderStyle.ACTIVE: StyleParams(6, 55, 2.0, 35, 4.0, 50, 30, 6),
}


@dataclass
class PresetDecision:
    action: Action
    reason: str
    met: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # SELLs only. ExitReason has no generic "strategy exit" value (contract request), so presets map onto the
    # closest one: Buy the Dip -> RSI_EXIT, Trend -> MACD_EXIT / TRAILING_STOP (EMA used as trailing line),
    # Grid top cell -> TAKE_PROFIT.
    exit_reason: Optional[ExitReason] = None


def select(
    regime: Regime, style: TraderStyle, enabled: Sequence[PresetName]
) -> tuple[Optional[PresetName], float, str]:
    prefs, mult = PRESET_MAP[regime][style]
    enabled_set = set(enabled)
    if not prefs:
        return None, 0.0, f"{regime.value} + {style.value}: policy is to pause / stay out"
    for k, preset in enumerate(prefs):
        if preset in enabled_set:
            why = "preferred preset" if k == 0 else f"fallback (preferred {prefs[0].value} is disabled)"
            return (
                preset,
                mult,
                f"{regime.value} + {style.value} -> {preset.value} ({why})"
                + (f", size x{mult:g}" if mult != 1.0 else ""),
            )
    names = ", ".join(p.value for p in prefs)
    if PresetName.DCA in enabled_set:
        # Last fallback: the user enabled DCA but none of the presets this regime/style prefers (e.g. only DCA on
        # ACTIVE, whose table never lists DCA). Run plain DCA at this regime's size multiplier. Pause regimes
        # (empty preference list, handled above) still pause.
        return (
            PresetName.DCA,
            mult,
            f"{regime.value} + {style.value} -> DCA (last fallback: none of the suitable presets ({names}) is "
            "enabled)" + (f", size x{mult:g}" if mult != 1.0 else ""),
        )
    return None, 0.0, f"{regime.value} + {style.value}: none of the suitable presets ({names}) is enabled; staying out"


def _slot(ts, bar_seconds: int, interval_bars: int) -> int:
    ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return int(ts.timestamp() // bar_seconds) // interval_bars


def _dca_due(candles: Sequence[Candle], sp: StyleParams, bar_seconds: int = 3600) -> bool:
    """Clock-aligned schedule (every ``dca_interval_bars`` bars of ``bar_seconds``): due on the FIRST bar at or after
    each slot boundary. With sparse real data (no bars while the market is closed) the buy happens on the first bar
    after the boundary instead of being skipped; with regular data it is exactly the boundary bar."""
    last = candles[-1].timestamp
    if len(candles) < 2:
        return int(last.timestamp() // bar_seconds) % sp.dca_interval_bars == 0
    return _slot(candles[-2].timestamp, bar_seconds, sp.dca_interval_bars) != _slot(
        last, bar_seconds, sp.dca_interval_bars
    )


def evaluate_preset(
    preset: PresetName, style: TraderStyle, candles: Sequence[Candle], holding: bool, bar_seconds: int = 3600
) -> PresetDecision:
    """Evaluate ONE preset on the last bar. ``holding`` = an open position exists for this symbol."""
    sp = STYLE_PARAMS[style]
    closes = [c.close for c in candles]
    i = len(candles) - 1
    close = closes[-1]
    rsi = ind.rsi(closes, 14)[-1]

    if preset == PresetName.DCA:
        if _dca_due(candles, sp, bar_seconds):
            return PresetDecision(
                Action.BUY, f"DCA: scheduled buy (every {sp.dca_interval_bars} bars)", ["dca_schedule"]
            )
        return PresetDecision(
            Action.STAY_OUT, f"DCA: not a scheduled bar (every {sp.dca_interval_bars} bars)", failed=["dca_schedule"]
        )

    if preset == PresetName.SMART_DCA:
        if not _dca_due(candles, sp, bar_seconds):
            return PresetDecision(
                Action.STAY_OUT,
                f"Smart DCA: not a scheduled bar (every {sp.dca_interval_bars} bars)",
                failed=["dca_schedule"],
            )
        sma20 = ind.sma(closes, 20)[-1]
        if rsi is None or sma20 is None:
            return PresetDecision(Action.STAY_OUT, "Smart DCA: indicators warming up", failed=["warmup"])
        cheap_rsi = rsi < sp.smart_dca_rsi_max
        dist = (close / sma20 - 1) * 100
        cheap_sma = dist <= sp.smart_dca_max_above_sma_pct
        met = ["dca_schedule"] + (["rsi_below"] if cheap_rsi else []) + (["near_sma20"] if cheap_sma else [])
        failed = ([] if cheap_rsi else ["rsi_below"]) + ([] if cheap_sma else ["near_sma20"])
        if cheap_rsi or cheap_sma:
            return PresetDecision(
                Action.BUY,
                f"Smart DCA: scheduled buy at a fair price (RSI {rsi:.1f}, {dist:+.2f}% vs SMA20)",
                met,
                failed,
            )
        return PresetDecision(
            Action.STAY_OUT,
            f"Smart DCA: skipping scheduled buy, price stretched (RSI {rsi:.1f} >= "
            f"{sp.smart_dca_rsi_max:g}, {dist:+.2f}% above SMA20)",
            met,
            failed,
        )

    if preset == PresetName.BUY_THE_DIP:
        if rsi is None or i < 20:
            return PresetDecision(Action.STAY_OUT, "Buy the Dip: indicators warming up", failed=["warmup"])
        if holding:
            if rsi > sp.dip_exit_rsi:
                return PresetDecision(
                    Action.SELL,
                    f"Buy the Dip: RSI recovered to {rsi:.1f} > {sp.dip_exit_rsi:g}, taking the bounce",
                    ["rsi_recovered"],
                    exit_reason=ExitReason.RSI_EXIT,
                )
            return PresetDecision(
                Action.STAY_OUT,
                f"Buy the Dip: holding, waiting for RSI > {sp.dip_exit_rsi:g} (now {rsi:.1f})",
                failed=["rsi_recovered"],
            )
        hi20 = max(c.high for c in candles[-20:])
        drop = (1 - close / hi20) * 100
        ok_rsi, ok_drop = rsi < sp.dip_rsi, drop >= sp.dip_drop_pct
        met = (["rsi_below"] if ok_rsi else []) + (["drop_from_high"] if ok_drop else [])
        failed = ([] if ok_rsi else ["rsi_below"]) + ([] if ok_drop else ["drop_from_high"])
        if ok_rsi and ok_drop:
            return PresetDecision(
                Action.BUY,
                f"Buy the Dip: RSI {rsi:.1f} < {sp.dip_rsi:g} after a {drop:.1f}% drop from the 20-bar high",
                met,
                failed,
            )
        return PresetDecision(
            Action.STAY_OUT,
            f"Buy the Dip: no dip (RSI {rsi:.1f}, drop {drop:.1f}%; need RSI < "
            f"{sp.dip_rsi:g} and drop >= {sp.dip_drop_pct:g}%)",
            met,
            failed,
        )

    if preset == PresetName.TREND_FOLLOWING:
        fast = ind.ema(closes, sp.trend_fast)[-1]
        slow = ind.sma(closes, sp.trend_slow)[-1]
        line, sig, _ = ind.macd(closes)
        if fast is None or slow is None or line[-1] is None or sig[-1] is None:
            return PresetDecision(Action.STAY_OUT, "Trend: indicators warming up", failed=["warmup"])
        macd_up = line[-1] > sig[-1]
        if holding:
            if close < fast or not macd_up:
                why = f"close below EMA{sp.trend_fast}" if close < fast else "MACD below signal"
                xr = ExitReason.TRAILING_STOP if close < fast else ExitReason.MACD_EXIT
                return PresetDecision(Action.SELL, f"Trend: momentum lost ({why})", ["trend_broken"], exit_reason=xr)
            return PresetDecision(Action.STAY_OUT, "Trend: holding, trend intact", failed=["trend_broken"])
        conds = {"close_above_ema": close > fast, "ema_above_sma": fast > slow, "macd_above_signal": macd_up}
        met = [k for k, v in conds.items() if v]
        failed = [k for k, v in conds.items() if not v]
        if not failed:
            return PresetDecision(
                Action.BUY, f"Trend: close > EMA{sp.trend_fast} > SMA{sp.trend_slow} and MACD above signal", met, failed
            )
        return PresetDecision(
            Action.STAY_OUT, "Trend: no confirmed uptrend (" + ", ".join(failed) + " failed)", met, failed
        )

    if preset == PresetName.GRID:
        if i < sp.grid_lookback:
            return PresetDecision(Action.STAY_OUT, "Grid: not enough history for the range", failed=["warmup"])
        window = candles[-sp.grid_lookback - 1 : -1]  # previous N bars (no look-ahead into the current bar)
        lo, hi = min(c.low for c in window), max(c.high for c in window)
        if hi <= lo:
            return PresetDecision(Action.STAY_OUT, "Grid: flat range", failed=["range"])
        step = (hi - lo) / sp.grid_levels
        level = int(min(max((close - lo) / step, 0), sp.grid_levels - 1e-9))  # 0 = bottom cell
        pos = f"cell {level + 1}/{sp.grid_levels} of range {lo:.4g}-{hi:.4g}"
        if holding and level >= sp.grid_levels - 1:
            return PresetDecision(
                Action.SELL, f"Grid: price in the top grid {pos}", ["top_cell"], exit_reason=ExitReason.TAKE_PROFIT
            )
        if level == 0 and not (close < lo):
            return PresetDecision(Action.BUY, f"Grid: price in the bottom grid {pos}", ["bottom_cell"])
        if close < lo:
            return PresetDecision(
                Action.STAY_OUT,
                f"Grid: price broke below the range ({close:.4g} < {lo:.4g}), not catching it",
                failed=["in_range"],
            )
        return PresetDecision(Action.STAY_OUT, f"Grid: price mid-range, {pos}", failed=["bottom_cell"])

    raise ValueError(f"unknown preset {preset!r}")  # pragma: no cover

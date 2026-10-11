"""Robustness on real-shaped on-chain tokenized-stock candles (sparse, gaps, zero volume, bad rows, tiny prices)."""

import math
import statistics
from datetime import datetime, timedelta, timezone

import pytest

from backend.contracts import (
    Action,
    AutoConfig,
    Candle,
    ConditionLogic,
    ManualStrategy,
    Portfolio,
    PresetName,
    Regime,
    Timeframe,
    TraderStyle,
)
from backend.engine import DefaultStrategyEngine
from backend.engine.data import (
    contiguous_pairs,
    infer_bar_seconds,
    infer_token_type,
    is_dead_bar,
    sanitize_candles,
)
from backend.engine.presets import STYLE_PARAMS, _dca_due
from backend.engine.regime import HIGH_VOL_STDEV_PCT, detect_regime, timeframe_scale
from backend.mocks import generate_candles

from .helpers import bar, entry_scenario, flat, sma_entry_strategy
from .real_fixtures import (
    MONDAY,
    dead_bars,
    real_shaped_candles,
    real_shaped_rows,
    round_the_clock_candles,
    rows_to_candles,
    session_hours_1h,
)

ENG = DefaultStrategyEngine()
T = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)


def pf(equity=1000.0):
    return Portfolio(starting_capital=1000, cash=equity, equity=equity)


def raw(i, o, h, lo, c, v=1.0, ts=None):
    """Unvalidated candle (what a buggy upstream could hand us)."""
    return Candle.model_construct(timestamp=ts or T + timedelta(hours=i), open=o, high=h, low=lo, close=c, volume=v)


# ---------------------------------------------------------------- sanitising
def test_sanitize_drops_bad_rows_and_repairs():
    good = [bar(i, 100, 101, 99, 100.5) for i in range(3)]
    junk = [
        None,
        raw(10, float("nan"), 1, 1, 1),
        raw(11, 100, 101, 99, -5),
        raw(12, 100, 99, 101, 100),  # high < low
        raw(13, 100, 101, 99, None),
        raw(14, 100, 101, 99, 100, ts="not-a-date"),
        raw(15, 100, float("inf"), 99, 100),
        raw(16, 0, 1, 0, 1),
    ]
    clean, rep = sanitize_candles(good + junk)
    assert clean == good
    assert rep.dropped_invalid == len(junk) and rep.kept == 3 and "dropped 8 invalid" in rep.issues[0]


def test_sanitize_volume_duplicates_order_dicts():
    a = raw(0, 100, 101, 99, 100, v=None)
    b = raw(1, 100, 101, 99, 100, v=float("nan"))
    c = raw(2, 100, 101, 99, 100, v=-3)
    d1 = bar(3, 100, 101, 99, 100, v=5)
    d2 = bar(3, 100, 102, 99, 101, v=7)  # re-sent in-progress bar: last wins
    e = {"timestamp": d1.timestamp + timedelta(hours=1), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    naive = raw(5, 100, 101, 99, 100, ts=datetime(2026, 1, 5, 5))
    clean, rep = sanitize_candles([d2, c, a, d1, b, e, naive][::-1])
    by_ts = {k.timestamp: k for k in clean}
    assert [by_ts[x.timestamp].volume for x in (a, b, c)] == [0.0, 0.0, 0.0] and rep.volume_missing == 3
    assert rep.duplicates == 1 and rep.unsorted
    assert all(x.timestamp < y.timestamp for x, y in zip(clean, clean[1:], strict=False))
    assert all(k.timestamp.tzinfo is not None for k in clean)
    assert by_ts[d1.timestamp] == d2  # the last copy seen wins (a re-sent bar replaces the earlier one)
    assert sanitize_candles([]) == ([], sanitize_candles([])[1]) and sanitize_candles(None)[0] == []


def test_sanitize_widens_inconsistent_high_low():
    c = raw(0, 100, 100.5, 99.5, 101)  # close above high
    (k,), _ = sanitize_candles([c])
    assert k.high == 101 and k.low == 99.5


def test_rows_like_api_strings_and_malformed():
    rows = real_shaped_rows(session_hours_1h(5))
    rows.insert(2, ["bad"])
    rows.append(rows[-1])  # duplicate row
    cs = rows_to_candles(rows)
    clean, rep = sanitize_candles(cs)
    assert rep.duplicates == 1 and len(clean) == len(cs) - 1


def test_infer_bar_seconds_robust_to_gaps():
    assert infer_bar_seconds(real_shaped_candles(days=20)) == 3600
    five = [bar(0, 1, 1, 1, 1)] + [
        Candle(timestamp=T + timedelta(minutes=5 * k), open=1, high=1, low=1, close=1, volume=1) for k in range(1, 40)
    ]
    assert infer_bar_seconds(five[1:]) == 300
    daily = [
        Candle(timestamp=MONDAY + timedelta(days=d), open=1, high=1, low=1, close=1, volume=1)
        for d in range(30)
        if (MONDAY + timedelta(days=d)).weekday() < 5
    ]
    assert infer_bar_seconds(daily) == 86_400
    assert infer_bar_seconds([]) == 3600


def test_token_type_inference():
    from backend.contracts import TokenType

    assert infer_token_type("NVDAon") == TokenType.ONDO
    assert infer_token_type("TSLAx") == TokenType.XSTOCK
    assert infer_token_type("AAPLB") == TokenType.BSTOCK
    assert infer_token_type("USDT") == TokenType.OTHER


# ---------------------------------------------------------------- regime on real-shaped data
def test_sparse_window_does_not_score_gaps_as_volatility():
    """Fewer than five gap-free returns must not be filled in from overnight jumps."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    cs = []
    for i in range(70):
        cs.append(Candle(timestamp=t0 + timedelta(hours=i), open=price, high=price * 1.001,
                         low=price * 0.999, close=price, volume=10))
    last = cs[-1].timestamp
    for i in range(1, 21):
        price *= 1.25
        ts = last + timedelta(days=2 * i)
        cs.append(Candle(timestamp=ts, open=price, high=price, low=price * 0.99, close=price, volume=10))
    ra = detect_regime(cs)
    assert ra.features["vol_used_gap_returns"] == 0
    assert ra.features["vol_returns_used"] == 0
    assert ra.features["volatility_pct"] == 0
    assert ra.regime != Regime.HIGH_VOLATILITY


def test_weekend_gaps_are_not_volatility():
    # every overnight/weekend gap opens 6% away (alternating); within a session the stock is calm
    cs = real_shaped_candles(days=40, gap_jump=0.06, gap_after=timedelta(hours=2), seed=3, noise=0.002)
    clean, rep = sanitize_candles(cs)
    assert rep.gaps > 10
    ra = detect_regime(cs)
    assert ra.regime != Regime.HIGH_VOLATILITY
    closes = [c.close for c in clean[-21:]]
    naive = statistics.pstdev([math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)]) * 100
    assert (
        naive > HIGH_VOL_STDEV_PCT > ra.features["volatility_pct"]
    )  # the naive estimate would have said HIGH_VOLATILITY
    pairs = contiguous_pairs(clean, 3600)
    assert len(pairs) < len(clean) - 1


def test_closed_market_flat_stretch_is_not_a_trend():
    up = real_shaped_candles(days=40, drift=0.004, noise=0.002, seed=1, zero_volume_prob=0.0)
    assert detect_regime(up).regime == Regime.STRONG_UPTREND
    ra = detect_regime(up + dead_bars(up[-1], 15))
    assert ra.regime == Regime.SIDEWAYS and ra.confidence <= 0.2 and ra.features["insufficient_data"] == 1
    assert all(is_dead_bar(c) for c in dead_bars(up[-1], 3))


def test_dead_bars_inside_history_are_ignored():
    up = real_shaped_candles(days=40, drift=0.004, noise=0.002, seed=1, zero_volume_prob=0.0)
    mixed = up[:100] + dead_bars(up[99], 3) + up[100:]
    mixed, _ = sanitize_candles(mixed)
    assert detect_regime(mixed).regime == Regime.STRONG_UPTREND


def test_timeframe_scaling_daily_noise_is_normal():
    # 3% daily moves are ordinary for a stock; on 1h bars they would be extreme.
    cs = generate_candles(Regime.HIGH_VOLATILITY, n=120, seed=5, timeframe=Timeframe.D1)
    cs = [c.model_copy(update={"volume": 1.0}) for c in cs]
    hourly = [c.model_copy(update={"timestamp": cs[0].timestamp + k * timedelta(hours=1)}) for k, c in enumerate(cs)]
    assert detect_regime(hourly).regime == Regime.HIGH_VOLATILITY
    assert detect_regime(cs).features["threshold_scale"] == pytest.approx(math.sqrt(24))
    assert detect_regime(cs).regime != Regime.HIGH_VOLATILITY
    # An explicit 1h timeframe changes the threshold scale. It does not turn the
    # day-long jumps between these candles into hourly volatility.
    forced = detect_regime(cs, Timeframe.H1)
    assert forced.features["threshold_scale"] == pytest.approx(1.0)
    assert forced.features["vol_used_gap_returns"] == 0
    assert forced.features["volatility_pct"] == 0
    assert forced.regime != Regime.HIGH_VOLATILITY
    assert timeframe_scale(60) == 0.25 and timeframe_scale(3600) == 1.0 and timeframe_scale(14_400) == 2.0


def test_tiny_prices_same_regime_and_full_precision():
    for regime in Regime:
        cs = generate_candles(regime, n=200, seed=42)
        tiny = [c.model_copy(update={k: getattr(c, k) * 1e-7 for k in ("open", "high", "low", "close")}) for c in cs]
        a, b = detect_regime(cs), detect_regime(tiny)
        assert a.regime == b.regime == regime
        assert b.features["close"] == pytest.approx(cs[-1].close * 1e-7) and b.features["close"] > 0


def test_too_few_and_invalid_bars():
    assert detect_regime(flat(30)).features["insufficient_data"] == 1
    ra = detect_regime([None, raw(0, float("nan"), 1, 1, 1)])
    assert ra.confidence == 0 and ra.features["insufficient_data"] == 1


# ---------------------------------------------------------------- manual evaluator on real-shaped data
def test_manual_insufficient_data_stays_out_with_reason():
    sig = ENG.evaluate_manual(sma_entry_strategy(), entry_scenario([], tail=0)[:40], pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["insufficient_data"]
    assert "40 valid candles" in sig.reason
    sig = ENG.evaluate_manual(sma_entry_strategy(), [None, raw(0, -1, 1, 1, 1)], pf())
    assert sig.action == Action.STAY_OUT and "No valid candles" in sig.reason


def test_manual_bad_rows_and_order_do_not_change_the_decision():
    cs = entry_scenario([], tail=0)
    clean_sig = ENG.evaluate_manual(sma_entry_strategy(), cs, pf())
    noisy = list(reversed(cs)) + [None, raw(5, float("nan"), 1, 1, 1), cs[-1]]
    sig = ENG.evaluate_manual(sma_entry_strategy(), noisy, pf())
    assert (
        (sig.action, sig.conditions_met)
        == (clean_sig.action, clean_sig.conditions_met)
        == (Action.BUY, ["price_above_sma"])
    )
    assert "data:" in sig.reason


def test_zero_volume_never_triggers_volume_entry_or_passes_filter():
    s = ManualStrategy(name="vol")
    s.entry.logic = ConditionLogic.OR
    s.entry.rsi_below.enabled = False
    s.entry.price_above_sma.enabled = False
    s.entry.volume_above_avg.enabled = True
    cs = flat(70, v=0.0)
    sig = ENG.evaluate_manual(s, cs, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["volume_above_avg"]
    assert "no volume data" in sig.reason
    s2 = sma_entry_strategy()
    s2.filters.min_volume.enabled = True
    cs2 = [c.model_copy(update={"volume": 0.0}) for c in entry_scenario([], tail=0)]
    sig = ENG.evaluate_manual(s2, cs2, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["min_volume"] and "no volume data" in sig.reason
    cs3 = cs2[:-1] + [cs2[-1].model_copy(update={"volume": 5.0})]  # spike vs an all-zero average
    assert ENG.evaluate_manual(s2, cs3, pf()).action == Action.STAY_OUT


def test_manual_on_real_shaped_series_never_crashes():
    s = ManualStrategy(name="real", symbol="NVDAon")
    s.entry.logic = ConditionLogic.OR
    s.entry.volume_above_avg.enabled = True
    s.entry.macd_bullish_cross.enabled = True
    s.entry.bollinger_lower.enabled = True
    s.filters.min_volume.enabled = True
    cs = real_shaped_candles(days=60, seed=11)
    for k in range(1, len(cs) + 1, 7):
        sig = ENG.evaluate_manual(s, cs[:k], pf())
        assert sig.action in (Action.BUY, Action.STAY_OUT)
        if sig.action == Action.BUY:
            assert cs[k - 1].volume > 0  # min-volume hard filter


# ---------------------------------------------------------------- auto on real-shaped data
@pytest.mark.parametrize("style", list(TraderStyle))
def test_auto_on_real_shaped_series(style):
    cs = real_shaped_candles(days=60, seed=4)
    cfg = AutoConfig(symbol="NVDAon", capital=200, style=style)
    for k in (10, 61, 120, len(cs)):
        sig = ENG.evaluate_auto(cfg, cs[:k], pf())
        assert sig.action in (Action.BUY, Action.STAY_OUT)
    assert ENG.evaluate_auto(cfg, cs[:10], pf()).conditions_failed == ["insufficient_data"]


def test_auto_stays_out_when_market_dead():
    up = real_shaped_candles(days=40, drift=0.004, noise=0.002, seed=1, zero_volume_prob=0.0)
    cfg = AutoConfig(symbol="NVDAon", capital=200, style=TraderStyle.ACTIVE, enabled_presets=[PresetName.DCA])
    for k in range(1, 15):
        sig = ENG.evaluate_auto(cfg, up + dead_bars(up[-1], k + 10), pf())
        assert sig.action == Action.STAY_OUT


def test_dca_slot_fires_after_closed_hours():
    sp = STYLE_PARAMS[TraderStyle.BALANCED]  # every 12 bars of 1h -> slots at 00:00 and 12:00 UTC
    times = session_hours_1h(3)  # 13:00-19:00 only: the 12:00 slot bar never exists
    cs = [Candle(timestamp=t, open=1, high=1, low=1, close=1, volume=1) for t in times]
    due = [cs[k].timestamp for k in range(1, len(cs)) if _dca_due(cs[: k + 1], sp, 3600)]
    assert due == [t for t in times[1:] if t.hour == 13]  # first bar after each 12:00 boundary


# ---------------------------------------------------------------- backtest on real-shaped data
def test_backtest_real_shaped_is_deterministic_and_aligned():
    cs = real_shaped_candles(days=80, seed=21, noise=0.006)
    s = ManualStrategy(name="bt", symbol="NVDAon")
    s.entry.logic = ConditionLogic.OR
    noisy = list(reversed(cs)) + [None, cs[5]]
    a = ENG.backtest_manual(s, cs, 1000)
    b = ENG.backtest_manual(s, noisy, 1000)
    assert a.model_dump() == b.model_dump()
    assert len(a.equity_curve) == len(cs)
    assert math.isfinite(a.final_equity) and a.num_trades > 0


def test_backtest_zero_volume_with_min_volume_filter_no_trades():
    cs = [c.model_copy(update={"volume": 0.0}) for c in real_shaped_candles(days=60, seed=2)]
    s = ManualStrategy(name="mv", symbol="NVDAon")
    s.entry.logic = ConditionLogic.OR
    s.filters.min_volume.enabled = True
    assert ENG.backtest_manual(s, cs, 1000).num_trades == 0


# ---------------------------------------------------------------- 24/7 data with thin overnight / weekend liquidity
def test_round_the_clock_data_regime_and_timeframe():
    cs = round_the_clock_candles(days=21, seed=9)
    clean, rep = sanitize_candles(cs)
    assert rep.gaps == 0 and rep.zero_volume > len(cs) / 3 and infer_bar_seconds(clean) == 3600
    for k in range(1, len(cs) + 1, 5):
        ra = detect_regime(cs[:k])
        assert math.isfinite(ra.confidence) and ra.regime in Regime
    # a flat 24/7 market with small noise is never a trend or high volatility
    assert detect_regime(cs).regime in (Regime.SIDEWAYS, Regime.OVERSOLD_PULLBACK)


@pytest.mark.parametrize("style", list(TraderStyle))
def test_round_the_clock_live_entries_are_small_and_skip_dead_hours(style):
    cs = round_the_clock_candles(days=21, seed=9, drift=0.0008)
    cfg = AutoConfig(symbol="NVDAB", capital=1000, style=style)
    buys, blocked = 0, 0
    for k in range(61, len(cs) + 1):
        now = cs[k - 1].timestamp + timedelta(minutes=30)
        eng = DefaultStrategyEngine(live_safe=True, clock=lambda now=now: now)
        sig = eng.evaluate_auto(cfg, cs[:k], pf())
        assert sig.action in (Action.BUY, Action.STAY_OUT)
        blocked += any(f in ("live_no_volume", "live_thin_liquidity") for f in sig.conditions_failed)
        if sig.action == Action.BUY:
            buys += 1
            assert cs[k - 1].volume > 0 and sig.proposed_size_pct <= 2.0 and sig.proposed_notional <= 20.0
            assert "thin liquidity" in sig.reason or sig.proposed_size_pct <= 2.0
    if style == TraderStyle.ACTIVE:
        assert buys > 0 and blocked > 0  # trades in liquid hours, refuses dead overnight / weekend bars

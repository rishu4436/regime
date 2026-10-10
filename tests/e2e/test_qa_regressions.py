"""Regression tests for the QA notes on the reference simulator (reference/full_strategy_builder_simulation.html).
They run against whatever StrategyEngine the app wires (real Bot 2 methods when implemented, fallback otherwise)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.app.fallbacks import FallbackStrategyEngine
from backend.app.indicators import rsi
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import Action, Candle, ManualStrategy, Portfolio, Regime
from backend.mocks import MockExchangeClient, generate_candles
from backend.monitoring import PerformanceTracker
from backend.contracts import EquityPoint

ENGINE = build_components(Settings(), exchange=MockExchangeClient()).engine
PF = Portfolio(starting_capital=10000, cash=10000, equity=10000)


def _candles(closes, start=datetime(2026, 1, 5, 0, tzinfo=timezone.utc), vols=None):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(timestamp=start + timedelta(hours=i), open=prev, high=max(prev, c) * 1.001,
                          low=min(prev, c) * 0.999, close=c, volume=(vols[i] if vols else 1000)))
        prev = c
    return out


def test_mock_data_is_seeded():
    assert generate_candles(Regime.SIDEWAYS, seed=9) == generate_candles(Regime.SIDEWAYS, seed=9)
    assert generate_candles(Regime.SIDEWAYS, seed=9) != generate_candles(Regime.SIDEWAYS, seed=10)


def test_rsi_has_no_loss_or_one_quirk():
    # strictly rising series: Wilder RSI is 100; the simulator's `loss || 1` gives a much lower value
    assert rsi([100 + i for i in range(40)], 14)[-1] == 100.0


def test_drawdown_counts_open_positions():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    curve = [EquityPoint(timestamp=t + timedelta(hours=i), equity=e) for i, e in enumerate([10000, 10000, 9000, 9500, 10200])]
    assert PerformanceTracker.max_drawdown_pct(curve, 10000) == 10.0  # unrealised dip counted


def _only_volume_or(rsi_threshold: float = 100, strategy_kw=None) -> ManualStrategy:
    s = ManualStrategy(symbol="NVDAon").model_dump()
    s["entry"]["logic"] = "OR"
    s["entry"]["rsi_below"].update(enabled=True, threshold=rsi_threshold)  # 100: true on a choppy series, 0: never
    s["entry"]["price_above_sma"]["enabled"] = False
    s["risk"]["cooldown_bars"] = 0
    s.update(strategy_kw or {})
    return ManualStrategy.model_validate(s)


def test_min_volume_is_a_hard_filter_even_with_or_logic():
    closes = [100 + (i % 5) for i in range(80)]
    vols = [1000] * 80  # last bar NOT above average -> hard filter must block despite OR logic
    s = _only_volume_or()
    s = s.model_copy(update={"filters": s.filters.model_copy(update={"min_volume": s.filters.min_volume.model_copy(update={"enabled": True})})})
    sig = ENGINE.evaluate_manual(s, _candles(closes, vols=vols), PF)
    assert sig.action == Action.STAY_OUT
    vols[-1] = 5000
    assert ENGINE.evaluate_manual(s, _candles(closes, vols=vols), PF).action == Action.BUY
    # ...and it can never TRIGGER an entry on its own (simulator pushed it into the OR list)
    s0 = _only_volume_or(rsi_threshold=0)
    s0 = s0.model_copy(update={"filters": s.filters})
    assert ENGINE.evaluate_manual(s0, _candles(closes, vols=vols), PF).action == Action.STAY_OUT


def test_market_hours_use_real_utc_13_20_weekdays():
    s = _only_volume_or()
    s = s.model_copy(update={"filters": s.filters.model_copy(update={"market_hours": s.filters.market_hours.model_copy(update={"enabled": True})})})
    closes = [100 + (i % 5) for i in range(80)]
    # 80 hourly bars from Monday 00:00 -> last bar Tuesday 07:00 UTC (outside session)
    assert ENGINE.evaluate_manual(s, _candles(closes), PF).action == Action.STAY_OUT
    # last bar at Monday 14:00 UTC (inside session)
    start = datetime(2026, 1, 5, 14, tzinfo=timezone.utc) - timedelta(hours=79)
    assert ENGINE.evaluate_manual(s, _candles(closes, start=start), PF).action == Action.BUY
    # Saturday 14:00 UTC (weekend)
    start = datetime(2026, 1, 10, 14, tzinfo=timezone.utc) - timedelta(hours=79)
    assert ENGINE.evaluate_manual(s, _candles(closes, start=start), PF).action == Action.STAY_OUT


def test_fallback_engine_detects_each_mock_regime():
    e = FallbackStrategyEngine()
    for r in Regime:
        assert e.detect_regime(generate_candles(r, n=200, seed=42)).regime == r

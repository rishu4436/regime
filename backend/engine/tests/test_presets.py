import pytest

from backend.contracts import (
    Action,
    AgentMode,
    AutoConfig,
    ExitReason,
    Portfolio,
    Position,
    PresetName,
    Regime,
    StrategySelection,
    TraderStyle,
)
from backend.engine import DefaultStrategyEngine
from backend.engine.presets import BASE_SIZE_PCT, PRESET_MAP, evaluate_preset
from backend.mocks import MockExchangeClient, generate_candles

ENG = DefaultStrategyEngine()
ALL = list(PresetName)


def pf(positions=()):
    return Portfolio(starting_capital=1000, cash=1000, equity=1000, positions=list(positions))


def test_map_covers_every_cell():
    for r in Regime:
        for s in TraderStyle:
            prefs, mult = PRESET_MAP[r][s]
            assert 0 <= mult <= 1
            assert len(set(prefs)) == len(prefs)


@pytest.mark.parametrize("regime", list(Regime))
@pytest.mark.parametrize("style", list(TraderStyle))
def test_select_returns_exactly_one_or_none(regime, style):
    sel = ENG.select_strategy(regime, style, ALL)
    assert isinstance(sel, StrategySelection)
    assert sel.preset is None or sel.preset in ALL
    assert sel.reason


def test_spec_mapping_examples():
    assert ENG.select_strategy(Regime.STRONG_UPTREND, TraderStyle.BALANCED, ALL).preset == PresetName.TREND_FOLLOWING
    assert ENG.select_strategy(Regime.MILD_UPTREND, TraderStyle.BALANCED, ALL).preset == PresetName.SMART_DCA
    assert ENG.select_strategy(Regime.SIDEWAYS, TraderStyle.BALANCED, ALL).preset == PresetName.GRID
    assert ENG.select_strategy(Regime.SIDEWAYS, TraderStyle.PATIENT, ALL).preset == PresetName.DCA
    assert ENG.select_strategy(Regime.OVERSOLD_PULLBACK, TraderStyle.ACTIVE, ALL).preset == PresetName.BUY_THE_DIP
    hv = ENG.select_strategy(Regime.HIGH_VOLATILITY, TraderStyle.ACTIVE, ALL)
    assert hv.preset == PresetName.GRID and hv.size_multiplier == 0.5
    assert ENG.select_strategy(Regime.HIGH_VOLATILITY, TraderStyle.BALANCED, ALL).preset is None
    dt = ENG.select_strategy(Regime.DOWNTREND, TraderStyle.PATIENT, ALL)
    assert dt.preset == PresetName.DCA and dt.size_multiplier == 0.25
    assert ENG.select_strategy(Regime.DOWNTREND, TraderStyle.ACTIVE, ALL).preset is None


def test_fallback_when_preferred_disabled():
    sel = ENG.select_strategy(Regime.STRONG_UPTREND, TraderStyle.BALANCED, [PresetName.DCA, PresetName.GRID])
    assert sel.preset == PresetName.DCA and "fallback" in sel.reason


def test_none_enabled_suitable_means_stay_out():
    sel = ENG.select_strategy(Regime.OVERSOLD_PULLBACK, TraderStyle.ACTIVE, [PresetName.GRID])
    assert sel.preset is None and sel.size_multiplier == 0.0


@pytest.mark.parametrize("regime", list(Regime))
@pytest.mark.parametrize("style", list(TraderStyle))
def test_dca_last_fallback(regime, style):
    prefs, mult = PRESET_MAP[regime][style]
    sel = ENG.select_strategy(regime, style, [PresetName.DCA])
    if not prefs:  # pause regimes still pause
        assert sel.preset is None and sel.size_multiplier == 0.0 and "pause" in sel.reason
    elif PresetName.DCA in prefs:  # normal table fallback, unchanged
        assert sel.preset == PresetName.DCA and "last fallback" not in sel.reason
    else:
        assert sel.preset == PresetName.DCA and sel.size_multiplier == mult and "last fallback" in sel.reason
    # without DCA enabled nothing changes: still "none of the suitable presets"
    others = [p for p in PresetName if p != PresetName.DCA and p not in prefs]
    if prefs and others:
        assert ENG.select_strategy(regime, style, others).preset is None


def test_table_choice_unchanged_when_a_preferred_preset_is_enabled():
    for regime in Regime:
        for style in TraderStyle:
            prefs, _ = PRESET_MAP[regime][style]
            for p in prefs:
                enabled = [p, PresetName.DCA]
                expected = next(x for x in prefs if x in enabled)  # first enabled preset in table order
                assert ENG.select_strategy(regime, style, enabled).preset == expected


@pytest.mark.parametrize("live", [False, True])
def test_dca_only_active_buys(live):
    from datetime import timedelta

    hits = []
    for regime in (Regime.STRONG_UPTREND, Regime.MILD_UPTREND, Regime.SIDEWAYS, Regime.OVERSOLD_PULLBACK):
        cs = generate_candles(regime, n=200, seed=42)
        cfg = AutoConfig(symbol="NVDAB", capital=1000, style=TraderStyle.ACTIVE, enabled_presets=[PresetName.DCA])
        for k in range(61, len(cs) + 1):
            now = cs[k - 1].timestamp + timedelta(minutes=30)
            eng = DefaultStrategyEngine(live_safe=live, clock=lambda now=now: now)
            sig = eng.evaluate_auto(cfg, cs[:k], Portfolio(starting_capital=1000, cash=1000, equity=1000))
            if sig.action == Action.BUY:
                assert sig.strategy == "DCA" and "last fallback" in sig.reason
                if live:
                    assert sig.proposed_size_pct <= 2.0 and sig.proposed_notional <= 20.0
                else:
                    assert sig.proposed_size_pct == pytest.approx(BASE_SIZE_PCT[TraderStyle.ACTIVE])
                hits.append(regime)
    assert hits, "DCA-only + ACTIVE never bought"
    # pause regimes stay out
    cs = generate_candles(Regime.DOWNTREND, n=200, seed=42)
    cfg = AutoConfig(symbol="NVDAB", capital=1000, style=TraderStyle.ACTIVE, enabled_presets=[PresetName.DCA])
    assert (
        DefaultStrategyEngine().evaluate_auto(cfg, cs, Portfolio(starting_capital=1000, cash=1000, equity=1000)).action
        == Action.STAY_OUT
    )


def test_evaluate_auto_runs_only_selected_preset():
    ex = MockExchangeClient()
    for sym in ["NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"]:
        cs = ex.get_candles(sym, limit=200)
        for style in TraderStyle:
            cfg = AutoConfig(symbol=sym, capital=1000, style=style)
            sig = ENG.evaluate_auto(cfg, cs, pf())
            ra = ENG.detect_regime(cs)
            sel = ENG.select_strategy(ra.regime, style, cfg.enabled_presets)
            assert sig.source == AgentMode.AUTO and sig.regime == ra.regime
            assert sig.preset == sel.preset
            assert sig.strategy == (sel.preset.value if sel.preset else "NONE")
            assert sig.action in (Action.BUY, Action.STAY_OUT, Action.SELL)
            assert sig.reason.startswith(f"Regime {ra.regime.value}")
            if sig.action == Action.BUY:
                assert sig.proposed_size_pct == pytest.approx(BASE_SIZE_PCT[style] * sel.size_multiplier)
                assert sig.proposed_notional == pytest.approx(1000 * sig.proposed_size_pct / 100)
            else:
                assert sig.proposed_notional == 0


def test_evaluate_auto_known_outcomes():
    ex = MockExchangeClient()
    dip = ENG.evaluate_auto(
        AutoConfig(symbol="METAx", capital=1000, style=TraderStyle.ACTIVE), ex.get_candles("METAx", limit=200), pf()
    )
    assert dip.action == Action.BUY and dip.preset == PresetName.BUY_THE_DIP
    paused = ENG.evaluate_auto(
        AutoConfig(symbol="TSLAx", capital=1000, style=TraderStyle.BALANCED), ex.get_candles("TSLAx", limit=200), pf()
    )
    assert paused.action == Action.STAY_OUT and paused.preset is None and "stay out" in paused.reason
    trend = ENG.evaluate_auto(
        AutoConfig(symbol="NVDAon", capital=1000, style=TraderStyle.BALANCED), ex.get_candles("NVDAon", limit=200), pf()
    )
    assert trend.action == Action.BUY and trend.preset == PresetName.TREND_FOLLOWING


def test_evaluate_auto_empty_candles():
    sig = ENG.evaluate_auto(AutoConfig(symbol="X", capital=10), [], pf())
    assert sig.action == Action.STAY_OUT


def test_dca_schedule_is_clock_aligned():
    cs = generate_candles(Regime.SIDEWAYS, n=100)
    buys = [
        i
        for i in range(60, 100)
        if evaluate_preset(PresetName.DCA, TraderStyle.BALANCED, cs[: i + 1], False).action == Action.BUY
    ]
    assert buys == [i for i in range(60, 100) if cs[i].timestamp.hour % 12 == 0]
    assert buys == [60, 72, 84, 96]


def test_dip_and_trend_sell_when_holding():
    oversold = generate_candles(Regime.OVERSOLD_PULLBACK, n=200)
    assert (
        evaluate_preset(PresetName.BUY_THE_DIP, TraderStyle.BALANCED, oversold, holding=True).action == Action.STAY_OUT
    )
    up = generate_candles(Regime.STRONG_UPTREND, n=200)
    assert evaluate_preset(PresetName.BUY_THE_DIP, TraderStyle.BALANCED, up, holding=True).action == Action.SELL
    down = generate_candles(Regime.DOWNTREND, n=200)
    assert evaluate_preset(PresetName.TREND_FOLLOWING, TraderStyle.ACTIVE, down, holding=True).action == Action.SELL
    assert evaluate_preset(PresetName.TREND_FOLLOWING, TraderStyle.ACTIVE, up, holding=True).action == Action.STAY_OUT


def test_grid_buys_low_sells_high():
    side = generate_candles(Regime.SIDEWAYS, n=200)
    results = {
        evaluate_preset(PresetName.GRID, TraderStyle.BALANCED, side[: i + 1], True).action for i in range(60, 200)
    }
    assert Action.BUY in results and Action.SELL in results


def test_auto_sell_notional_uses_held_quantity():
    cs = generate_candles(Regime.STRONG_UPTREND, n=200)
    pos = Position(symbol="NVDAon", quantity=2, avg_entry_price=100, strategy="BUY_THE_DIP")
    # strong uptrend + ACTIVE with only BUY_THE_DIP enabled -> not suitable -> stay out (single strategy only)
    sig = ENG.evaluate_auto(
        AutoConfig(symbol="NVDAon", capital=1000, style=TraderStyle.ACTIVE, enabled_presets=[PresetName.BUY_THE_DIP]),
        cs,
        pf([pos]),
    )
    assert sig.action == Action.STAY_OUT
    sig = ENG.evaluate_auto(
        AutoConfig(
            symbol="NVDAon", capital=1000, style=TraderStyle.BALANCED, enabled_presets=[PresetName.TREND_FOLLOWING]
        ),
        cs,
        pf([pos]),
    )
    assert sig.action == Action.STAY_OUT  # holding, trend intact
    side = generate_candles(Regime.SIDEWAYS, n=200)
    cfg = AutoConfig(symbol="NVDAon", capital=1000, style=TraderStyle.BALANCED, enabled_presets=[PresetName.GRID])
    sells = [ENG.evaluate_auto(cfg, side[: i + 1], pf([pos])) for i in range(80, 200)]
    sells = [(k, s) for k, s in enumerate(sells) if s.action == Action.SELL]
    assert sells, "expected at least one GRID sell while holding"
    k, s2 = sells[0]
    assert s2.proposed_size_pct == 100.0
    assert s2.position_id == pos.id and s2.exit_reason == ExitReason.TAKE_PROFIT and s2.price == side[80 + k].close
    assert s2.proposed_notional == pytest.approx(2 * side[80 + k].close)

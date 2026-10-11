from datetime import timedelta

import pytest

from backend.contracts import (
    Action, AgentMode, Candle, ExitReason, ExitRules, PositionRiskRules, TrailingStopRule,
)
from backend.mocks import MockExchangeClient
from backend.risk import DefaultRiskManager, RiskConfig, SymbolRiskRules, is_risk_exit

from .conftest import T0, make_portfolio, pos

TRAIL_CFG = RiskConfig(exit_rules=ExitRules(trailing_stop=TrailingStopRule(enabled=True, pct=4.0)))


def candle(o, h, l, c, i=0):
    return Candle(timestamp=T0 + timedelta(hours=i), open=o, high=h, low=l, close=c, volume=1000)


def app_update_highest(portfolio, bars, closed_ids):
    """What backend/app does AFTER check_exits (contract v0.2): raise highest_price from bar.high."""
    return portfolio.model_copy(update={"positions": [
        p.model_copy(update={"highest_price": max(p.highest_price or p.avg_entry_price, bars[p.symbol].high)})
        for p in portfolio.positions if p.id not in closed_ids]})


def test_no_positions_no_signals():
    assert DefaultRiskManager().check_exits(make_portfolio(), {"NVDAon": 1.0}) == []


def test_bars_argument_is_optional_keyword():
    p = make_portfolio(cash=9000, positions=[pos()])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 101.0}, None) == []
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 101.0}, bars={}) == []


def test_price_stop_loss_signal_shape(clock):
    rm = DefaultRiskManager(clock=clock)
    position = pos(qty=10, entry=100, strategy="MANUAL:dip")
    p = make_portfolio(cash=9000, positions=[position])
    [s] = rm.check_exits(p, {"NVDAon": 93.0})
    assert s.action == Action.SELL and s.symbol == "NVDAon" and s.source == AgentMode.MANUAL and s.strategy == "MANUAL:dip"
    assert s.exit_reason == ExitReason.STOP_LOSS and s.position_id == position.id
    assert s.price == 93.0 and s.proposed_notional == pytest.approx(930.0) and s.timestamp == clock()
    assert is_risk_exit(s) and "STOP_LOSS" in s.reason and "PnL -7.00%" in s.reason


def test_one_signal_per_position_with_its_id():
    a, b = pos(entry=100), pos(entry=100)
    p = make_portfolio(cash=8000, positions=[a, b])
    sigs = DefaultRiskManager().check_exits(p, {"NVDAon": 90.0})
    assert [s.position_id for s in sigs] == [a.id, b.id]


def test_auto_source_inferred():
    p = make_portfolio(cash=9000, positions=[pos(strategy="TREND_FOLLOWING")])
    s = DefaultRiskManager().check_exits(p, {"NVDAon": 120.0})[0]
    assert s.source == AgentMode.AUTO and s.exit_reason == ExitReason.TAKE_PROFIT


def test_inside_band_no_signal_and_missing_price_skipped():
    p = make_portfolio(cash=8000, positions=[pos(), pos("TSLAx", entry=200)])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 101.0}) == []


def test_current_price_used_when_price_missing():
    p = make_portfolio(cash=9000, positions=[pos(current_price=80.0)])
    assert DefaultRiskManager().check_exits(p, {})[0].exit_reason == ExitReason.STOP_LOSS


def test_bars_intrabar_vs_prices_only():
    rm = DefaultRiskManager()
    p = make_portfolio(cash=9000, positions=[pos()])
    bar = candle(100, 101, 93, 99)
    assert rm.check_exits(p, {"NVDAon": bar.close}) == []  # close-only (reference sim) misses the wick
    [s] = rm.check_exits(p, {"NVDAon": bar.close}, bars={"NVDAon": bar})
    assert s.exit_reason == ExitReason.STOP_LOSS and s.price == pytest.approx(94.0) and s.timestamp == bar.timestamp


def test_bars_take_priority_over_prices_and_prices_fill_gaps():
    rm = DefaultRiskManager()
    p = make_portfolio(cash=8000, positions=[pos(), pos("TSLAx", entry=200)])
    sigs = rm.check_exits(p, {"NVDAon": 100.0, "TSLAx": 180.0}, bars={"NVDAon": candle(100, 113, 99, 105)})
    assert {s.symbol: s.exit_reason for s in sigs} == {"NVDAon": ExitReason.TAKE_PROFIT, "TSLAx": ExitReason.STOP_LOSS}


def test_bars_tp_sl_same_bar_sl_wins():
    p = make_portfolio(cash=9000, positions=[pos()])
    [s] = DefaultRiskManager().check_exits(p, {}, bars={"NVDAon": candle(100, 115, 90, 110)})
    assert s.exit_reason == ExitReason.STOP_LOSS and s.price == pytest.approx(94.0)


def test_bars_gaps_fill_at_open():
    p = make_portfolio(cash=9000, positions=[pos()])
    rm = DefaultRiskManager()
    [s] = rm.check_exits(p, {}, bars={"NVDAon": candle(89, 90, 88, 89.5)})
    assert s.exit_reason == ExitReason.STOP_LOSS and s.price == 89
    [s] = rm.check_exits(p, {}, bars={"NVDAon": candle(120, 121, 119, 120)})
    assert s.exit_reason == ExitReason.TAKE_PROFIT and s.price == 120


def test_check_exits_does_not_mutate_portfolio():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos(highest_price=105.0), pos("TSLAx", entry=200)])
    before = p.model_dump()
    rm.check_exits(p, {"TSLAx": 150.0}, bars={"NVDAon": candle(106, 130, 104, 128)})
    rm.check_exits(p, {"NVDAon": 50.0, "TSLAx": 300.0})
    rm.emergency_stop()
    rm.check_exits(p, {"NVDAon": 1.0})
    assert p.model_dump() == before


def test_stateless_same_input_same_output():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos()])
    bars = {"NVDAon": candle(100, 111, 99, 110)}  # high 111 would tighten the trail if stored internally
    assert rm.check_exits(p, {}, bars=bars) == [] and rm.check_exits(p, {}, bars=bars) == []
    assert rm.check_exits(p, {"NVDAon": 97.0}) == []  # trail still entry*0.96 = 96: nothing remembered


def test_bar_own_high_never_tightens_its_own_stop():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos(highest_price=105.0)])  # trail 100.8 from prior bars
    assert rm.check_exits(p, {}, bars={"NVDAon": candle(106, 111, 101, 110)}) == []  # low 101 > 100.8


def test_trailing_across_bars_with_app_updating_highest_price():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos()])
    seq = [candle(100, 110, 97, 108, 1), candle(108, 111.5, 105.7, 111, 2), candle(110, 110.5, 106, 107, 3)]
    for i, bar in enumerate(seq):
        bars = {"NVDAon": bar}
        sigs = rm.check_exits(p, {}, bars=bars)
        if i < 2:
            assert sigs == []
            p = app_update_highest(p, bars, set())
        else:
            [s] = sigs
            assert s.exit_reason == ExitReason.TRAILING_STOP and s.price == pytest.approx(111.5 * 0.96)
    assert p.positions[0].highest_price == 111.5
    assert rm.exit_levels(p.positions[0]).trailing_stop == pytest.approx(107.04)


def test_trailing_with_prices_matches_simulator():
    # simulator: ratchet trail to close*(1-pct) then exit if close < trail; app keeps highest_price = max(close)
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos()])
    for px in (104, 108, 110):
        assert rm.check_exits(p, {"NVDAon": px}) == []
        p = app_update_highest(p, {"NVDAon": candle(px, px, px, px)}, set())
    [s] = rm.check_exits(p, {"NVDAon": 105.5})  # trail 105.6
    assert s.exit_reason == ExitReason.TRAILING_STOP and s.price == 105.5


def test_position_highest_price_seeds_trail():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos(highest_price=110.0)])
    assert rm.check_exits(p, {"NVDAon": 105.0})[0].exit_reason == ExitReason.TRAILING_STOP


def test_position_trailing_pct_enables_trailing_without_rule():
    p = make_portfolio(cash=9000, positions=[pos(highest_price=110.0, trailing_stop_pct=2.0)])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 107.0})[0].exit_reason == ExitReason.TRAILING_STOP


def test_max_loss_exit_off_by_default_and_on_when_set():
    p = make_portfolio(cash=9000, positions=[pos(qty=10, entry=100)])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 96.0}) == []
    rm = DefaultRiskManager(config=RiskConfig(position_rules=PositionRiskRules(max_loss_per_trade_pct=0.3)))
    [s] = rm.check_exits(p, {"NVDAon": 96.0})  # 30 USDT max loss / 10 units -> level 97
    assert s.exit_reason == ExitReason.MAX_LOSS and s.price == 96.0


def test_emergency_stop_closes_everything(clock):
    rm = DefaultRiskManager(clock=clock)
    a, b = pos(), pos("TSLAx", entry=200, current_price=210)
    p = make_portfolio(cash=7000, positions=[a, b])
    rm.emergency_stop("exchange incident")
    sigs = rm.check_exits(p, {"NVDAon": 101.0})
    assert {s.position_id for s in sigs} == {a.id, b.id}
    assert all(s.exit_reason == ExitReason.EMERGENCY_STOP and is_risk_exit(s) for s in sigs)
    assert {s.symbol: s.price for s in sigs} == {"NVDAon": 101.0, "TSLAx": 210}
    assert "exchange incident" in sigs[0].reason
    [s] = rm.check_exits(make_portfolio(cash=9000, positions=[a]), {}, bars={"NVDAon": candle(100, 102, 98, 99)})
    assert s.price == 99  # emergency exits at the bar close (market)


def test_paused_still_enforces_stops():
    rm = DefaultRiskManager()
    rm.pause()
    assert rm.check_exits(make_portfolio(cash=9000, positions=[pos()]), {"NVDAon": 90.0})


def test_symbol_exit_rules_override():
    rm = DefaultRiskManager()
    rm.set_symbol_rules("NVDAon", SymbolRiskRules(exit_rules=ExitRules(stop_loss_pct=2.0)))
    s = rm.check_exits(make_portfolio(cash=9000, positions=[pos()]), {"NVDAon": 97.9})[0]
    assert s.exit_reason == ExitReason.STOP_LOSS


def test_deprecated_alias_matches():
    p = make_portfolio(cash=9000, positions=[pos()])
    bar = candle(100, 101, 93, 99)
    rm = DefaultRiskManager()
    [a] = rm.check_exits_candles(p, {"NVDAon": bar})
    [b] = rm.check_exits(p, {}, bars={"NVDAon": bar})
    assert (a.exit_reason, a.price, a.position_id) == (b.exit_reason, b.price, b.position_id)


def test_deterministic_with_mock_exchange_candles():
    candles = MockExchangeClient().get_candles("TSLAx", limit=100)  # high-volatility regime

    def run():
        rm = DefaultRiskManager(config=TRAIL_CFG)
        p = make_portfolio(cash=9000, positions=[pos("TSLAx", qty=5, entry=candles[0].close)])
        for i, c in enumerate(candles[1:], 1):
            out = rm.check_exits(p, {"TSLAx": c.close}, bars={"TSLAx": c})
            if out:
                return i, out[0].price, out[0].exit_reason
            p = app_update_highest(p, {"TSLAx": c}, set())
        return None

    first = run()
    assert first is not None and first == run()

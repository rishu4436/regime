import pytest

from backend.contracts import (
    Action, AgentMode, Candle, ExitReason, ExitRules, PositionRiskRules, TrailingStopRule,
)
from backend.mocks import MockExchangeClient
from backend.risk import RISK_EXIT_TAG, DefaultRiskManager, RiskConfig, exit_reason_of, is_risk_exit

from .conftest import T0, make_portfolio, pos

TRAIL_CFG = RiskConfig(exit_rules=ExitRules(trailing_stop=TrailingStopRule(enabled=True, pct=4.0)))


def candle(o, h, l, c, i=0):
    from datetime import timedelta
    return Candle(timestamp=T0 + timedelta(hours=i), open=o, high=h, low=l, close=c, volume=1000)


def test_no_positions_no_signals():
    assert DefaultRiskManager().check_exits(make_portfolio(), {"NVDAon": 1.0}) == []


def test_price_stop_loss_signal_shape(clock):
    rm = DefaultRiskManager(clock=clock)
    p = make_portfolio(cash=9000, positions=[pos(qty=10, entry=100, strategy="MANUAL:dip")])
    [s] = rm.check_exits(p, {"NVDAon": 93.0})
    assert s.action == Action.SELL and s.symbol == "NVDAon" and s.source == AgentMode.MANUAL and s.strategy == "MANUAL:dip"
    assert s.price == 93.0 and s.proposed_notional == pytest.approx(930.0) and s.timestamp == clock()
    assert RISK_EXIT_TAG in s.conditions_met and exit_reason_of(s) == ExitReason.STOP_LOSS and is_risk_exit(s)
    assert "STOP_LOSS" in s.reason and "PnL -7.00%" in s.reason


def test_auto_source_inferred():
    p = make_portfolio(cash=9000, positions=[pos(strategy="TREND_FOLLOWING")])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 120.0})[0].source == AgentMode.AUTO


def test_inside_band_no_signal_and_missing_price_skipped():
    rm = DefaultRiskManager()
    p = make_portfolio(cash=8000, positions=[pos(), pos("TSLAx", entry=200)])
    assert rm.check_exits(p, {"NVDAon": 101.0}) == []


def test_current_price_used_when_price_missing():
    p = make_portfolio(cash=9000, positions=[pos(current_price=80.0)])
    assert exit_reason_of(DefaultRiskManager().check_exits(p, {})[0]) == ExitReason.STOP_LOSS


def test_candle_intrabar_vs_close_only():
    rm = DefaultRiskManager()
    p = make_portfolio(cash=9000, positions=[pos()])
    bar = candle(100, 101, 93, 99)
    assert rm.check_exits(p, {"NVDAon": bar.close}) == []  # close-only (reference sim) misses the wick
    [s] = rm.check_exits_candles(p, {"NVDAon": bar})
    assert exit_reason_of(s) == ExitReason.STOP_LOSS and s.price == pytest.approx(94.0) and s.timestamp == bar.timestamp


def test_candle_tp_sl_same_bar_sl_wins():
    p = make_portfolio(cash=9000, positions=[pos()])
    [s] = DefaultRiskManager().check_exits_candles(p, {"NVDAon": candle(100, 115, 90, 110)})
    assert exit_reason_of(s) == ExitReason.STOP_LOSS and s.price == pytest.approx(94.0)


def test_candle_gaps_fill_at_open():
    p = make_portfolio(cash=9000, positions=[pos()])
    rm = DefaultRiskManager()
    [s] = rm.check_exits_candles(p, {"NVDAon": candle(89, 90, 88, 89.5)})
    assert exit_reason_of(s) == ExitReason.STOP_LOSS and s.price == 89
    [s] = rm.check_exits_candles(p, {"NVDAon": candle(120, 121, 119, 120)})
    assert exit_reason_of(s) == ExitReason.TAKE_PROFIT and s.price == 120


def test_trailing_state_persists_across_bars_and_checks_previous_trail():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos()])
    # bar 1: high 110 raises hwm (trail for next bar 105.6); low 97 > first trail 96 -> no exit
    assert rm.check_exits_candles(p, {"NVDAon": candle(100, 110, 97, 108, 1)}) == []
    assert rm.exit_levels(p.positions[0]).trailing_stop == pytest.approx(105.6)
    # bar 2: new high 111.5 but low 105.7 >= previous trail 105.6 -> survive, hwm 111.5
    assert rm.check_exits_candles(p, {"NVDAon": candle(108, 111.5, 105.7, 111, 2)}) == []
    # bar 3: trail 107.04; low 106 -> trailing stop at 107.04
    [s] = rm.check_exits_candles(p, {"NVDAon": candle(110, 110.5, 106, 107, 3)})
    assert exit_reason_of(s) == ExitReason.TRAILING_STOP and s.price == pytest.approx(111.5 * 0.96)
    assert rm._hwm == {}  # state dropped after exit


def test_trailing_with_prices_matches_simulator():
    # simulator: ratchet trail to close*(1-pct) then exit if close < trail - with prices only this is equivalent
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos()])
    for px in (104, 108, 110):
        assert rm.check_exits(p, {"NVDAon": px}) == []
    [s] = rm.check_exits(p, {"NVDAon": 105.5})  # trail 105.6
    assert exit_reason_of(s) == ExitReason.TRAILING_STOP and s.price == 105.5


def test_position_highest_price_seeds_trail():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    p = make_portfolio(cash=9000, positions=[pos(highest_price=110.0)])
    assert exit_reason_of(rm.check_exits(p, {"NVDAon": 105.0})[0]) == ExitReason.TRAILING_STOP


def test_position_trailing_pct_enables_trailing_without_rule():
    p = make_portfolio(cash=9000, positions=[pos(highest_price=110.0, trailing_stop_pct=2.0)])
    assert exit_reason_of(DefaultRiskManager().check_exits(p, {"NVDAon": 107.0})[0]) == ExitReason.TRAILING_STOP


def test_hwm_pruned_for_closed_positions():
    rm = DefaultRiskManager(config=TRAIL_CFG)
    rm.check_exits(make_portfolio(cash=9000, positions=[pos()]), {"NVDAon": 105})
    assert len(rm._hwm) == 1
    rm.check_exits(make_portfolio(), {})
    assert rm._hwm == {}


def test_max_loss_exit_off_by_default_and_on_when_set():
    p = make_portfolio(cash=9000, positions=[pos(qty=10, entry=100)])
    assert DefaultRiskManager().check_exits(p, {"NVDAon": 96.0}) == []
    rm = DefaultRiskManager(config=RiskConfig(position_rules=PositionRiskRules(max_loss_per_trade_pct=0.3)))
    [s] = rm.check_exits(p, {"NVDAon": 96.0})  # 30 USDT max loss / 10 units -> level 97
    assert exit_reason_of(s) == ExitReason.MAX_LOSS


def test_emergency_stop_closes_everything(clock):
    rm = DefaultRiskManager(clock=clock)
    p = make_portfolio(cash=7000, positions=[pos(), pos("TSLAx", entry=200, current_price=210)])
    rm.emergency_stop("exchange incident")
    sigs = rm.check_exits(p, {"NVDAon": 101.0})
    assert {s.symbol for s in sigs} == {"NVDAon", "TSLAx"}
    assert all(exit_reason_of(s) == ExitReason.EMERGENCY_STOP for s in sigs)
    assert {s.symbol: s.price for s in sigs} == {"NVDAon": 101.0, "TSLAx": 210}
    assert "exchange incident" in sigs[0].reason


def test_paused_still_enforces_stops():
    rm = DefaultRiskManager()
    rm.pause()
    assert rm.check_exits(make_portfolio(cash=9000, positions=[pos()]), {"NVDAon": 90.0})


def test_symbol_exit_rules_override():
    from backend.risk import SymbolRiskRules
    rm = DefaultRiskManager()
    rm.set_symbol_rules("NVDAon", SymbolRiskRules(exit_rules=ExitRules(stop_loss_pct=2.0)))
    assert exit_reason_of(rm.check_exits(make_portfolio(cash=9000, positions=[pos()]), {"NVDAon": 97.9})[0]) == ExitReason.STOP_LOSS


def test_deterministic_with_mock_exchange_candles():
    ex = MockExchangeClient()
    candles = ex.get_candles("TSLAx", limit=100)  # high-volatility regime

    def run():
        rm = DefaultRiskManager(config=TRAIL_CFG)
        p = make_portfolio(cash=9000, positions=[pos("TSLAx", qty=5, entry=candles[0].close)])
        for i, c in enumerate(candles[1:], 1):
            out = rm.check_exits_candles(p, {"TSLAx": c})
            if out:
                return i, out[0].price, exit_reason_of(out[0])
        return None

    first = run()
    assert first is not None and first == run()

import pytest

from backend.contracts import (
    Action, ActivationMode, AgentMode, ControlState, ExitRules, ManualStrategy, PositionRiskRules, Signal, TradingMode,
)
from backend.risk import DefaultRiskManager, RiskConfig, SymbolRiskRules

from .conftest import buy, ctl, make_portfolio, pos, sell


def names(v):
    return {c.name: c for c in v.checks}


@pytest.fixture
def rm(clock):
    # confirmation off on BOTH the manager and the caller state (the stricter of the two wins)
    return DefaultRiskManager(ControlState(confirmation_mode=False), clock=clock)


def test_every_verdict_has_named_checks_and_reasons(rm):
    for sig in (buy(), sell(), Signal(symbol="NVDAon", action=Action.STAY_OUT, source=AgentMode.AUTO, strategy="DCA", reason="x")):
        v = rm.check(sig, make_portfolio(positions=[pos()]), ctl())
        assert v.signal_id == sig.id and v.reasons and v.checks
        assert all(c.name and c.detail for c in v.checks)


def test_buy_default_size_is_15pct_of_equity(rm):
    v = rm.check(buy(), make_portfolio(), ctl())
    assert v.approved and not v.requires_confirmation
    assert v.adjusted_notional == pytest.approx(1500.0) and v.adjusted_size_pct == pytest.approx(15.0)
    assert "Approved BUY NVDAon" in v.reasons[0]
    expected = {"signal_action", "trading_mode", "emergency_stop", "paused", "max_open_positions", "cooldown",
                "daily_loss_limit", "max_trades_per_day", "position_size", "cash_available", "max_token_exposure",
                "max_total_exposure", "max_loss_per_trade", "min_order_notional", "confirmation"}
    assert expected == set(names(v)) and all(c.passed for c in v.checks)


def test_stay_out_never_approved(rm):
    v = rm.check(Signal(symbol="NVDAon", action=Action.STAY_OUT, source=AgentMode.AUTO, strategy="DCA", reason="downtrend"),
                 make_portfolio(), ctl())
    assert not v.approved and v.adjusted_notional == 0 and "STAY_OUT" in v.reasons[0] and "downtrend" in v.reasons[0]


def test_signal_size_used_but_capped(rm):
    v = rm.check(buy(proposed_size_pct=5), make_portfolio(), ctl())
    assert v.adjusted_notional == pytest.approx(500)
    v = rm.check(buy(proposed_size_pct=50), make_portfolio(), ctl())
    assert v.adjusted_notional == pytest.approx(1500) and "capped" in names(v)["position_size"].detail
    v = rm.check(buy(proposed_notional=800), make_portfolio(), ctl())
    assert v.adjusted_notional == pytest.approx(800)


def test_paused_blocks_buy_with_reason(rm):
    v = rm.check(buy(), make_portfolio(), ctl(paused=True))
    assert not v.approved and v.adjusted_notional == 0
    assert not names(v)["paused"].passed and any("paused" in r for r in v.reasons)


def test_internal_pause_applies_even_if_caller_state_is_stale(rm):
    rm.pause("manual")
    assert not rm.check(buy(), make_portfolio(), ctl()).approved


def test_emergency_stop_blocks_buy(rm):
    rm.emergency_stop("panic")
    v = rm.check(buy(), make_portfolio(), ctl())
    assert not v.approved and not names(v)["emergency_stop"].passed


def test_live_blocked_by_default(rm):
    v = rm.check(buy(), make_portfolio(), ctl(trading_mode=TradingMode.LIVE))
    assert not v.approved and "LIVE" in names(v)["trading_mode"].detail
    v = rm.check(sell(), make_portfolio(positions=[pos()]), ctl(trading_mode=TradingMode.LIVE))
    assert not v.approved
    rm2 = DefaultRiskManager(config=RiskConfig(allow_live=True))
    assert rm2.check(buy(), make_portfolio(), ctl(trading_mode=TradingMode.LIVE)).approved


def test_max_open_positions(rm):
    v = rm.check(buy("TSLAx"), make_portfolio(cash=9000, positions=[pos()]), ctl())
    assert not v.approved and "limit reached" in names(v)["max_open_positions"].detail
    rm.set_config(RiskConfig(position_rules=PositionRiskRules(max_open_positions=3)))
    assert rm.check(buy("TSLAx"), make_portfolio(cash=9000, positions=[pos()]), ctl()).approved


@pytest.mark.parametrize("bars,ok", [(None, True), (0, False), (5, False), (6, True)])
def test_cooldown_matches_simulator(rm, bars, ok):
    # simulator: entry allowed when i - lastExit > cooldown (5)
    bse = {} if bars is None else {"NVDAon": bars}
    v = rm.check(buy(), make_portfolio(bars_since_exit=bse), ctl())
    assert v.approved is ok and names(v)["cooldown"].passed is ok


def test_cooldown_is_per_symbol(rm):
    assert rm.check(buy("TSLAx"), make_portfolio(bars_since_exit={"NVDAon": 0}), ctl()).approved


def test_daily_loss_limit(rm):
    assert rm.check(buy(), make_portfolio(daily_pnl=-499), ctl()).approved
    v = rm.check(buy(), make_portfolio(daily_pnl=-500), ctl())  # 5% of 10k
    assert not v.approved and "daily loss limit hit" in names(v)["daily_loss_limit"].detail
    rm.set_config(RiskConfig(daily_loss_limit_pct=None))
    assert rm.check(buy(), make_portfolio(daily_pnl=-5000), ctl()).approved


def test_max_trades_per_day(rm):
    rm.set_config(RiskConfig(max_trades_per_day=3))
    assert rm.check(buy(), make_portfolio(trades_today=2), ctl()).approved
    assert not rm.check(buy(), make_portfolio(trades_today=3), ctl()).approved


def test_cash_caps_size(rm):
    v = rm.check(buy(), make_portfolio(cash=400, equity=10000), ctl())
    assert v.approved and v.adjusted_notional == pytest.approx(400) and any("reduced" in r for r in v.reasons)


def test_token_exposure_cap(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False),
                            config=RiskConfig(position_rules=PositionRiskRules(max_open_positions=3)), clock=clock)
    p = make_portfolio(cash=7500, positions=[pos(qty=25, entry=100)])  # 2500 NVDAon of 10000 equity, cap 30% = 3000
    v = rm.check(buy(), p, ctl())
    assert v.approved and v.adjusted_notional == pytest.approx(500)
    assert "reduced" in names(v)["max_token_exposure"].detail
    p = make_portfolio(cash=7000, positions=[pos(qty=30, entry=100)])
    v = rm.check(buy(), p, ctl())
    assert not v.approved and not names(v)["min_order_notional"].passed


def test_total_exposure_cap(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False),
                            config=RiskConfig(position_rules=PositionRiskRules(max_open_positions=3)), clock=clock)
    p = make_portfolio(cash=5000, positions=[pos("TSLAx", qty=25, entry=100), pos("AAPLB", qty=25, entry=100)])
    v = rm.check(buy(), p, ctl())  # 5000 open, cap 60% = 6000 -> 1000
    assert v.approved and v.adjusted_notional == pytest.approx(1000)
    assert "reduced" in names(v)["max_total_exposure"].detail


def test_exposure_uses_current_price(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False),
                            config=RiskConfig(position_rules=PositionRiskRules(max_open_positions=3)), clock=clock)
    p = make_portfolio(cash=7500, positions=[pos(qty=25, entry=100, current_price=116)])  # 2900 now
    v = rm.check(buy(), p, ctl())
    assert v.adjusted_notional == pytest.approx(0.3 * p.equity - 2900)


def test_max_loss_per_trade_off_by_default_and_shrinks_size(rm):
    assert ManualStrategy().risk.max_loss_per_trade_pct is None
    v = rm.check(buy(), make_portfolio(), ctl())
    assert "off" in names(v)["max_loss_per_trade"].detail
    rm.set_config(RiskConfig(position_rules=PositionRiskRules(max_loss_per_trade_pct=0.6)))
    v = rm.check(buy(), make_portfolio(), ctl())  # 60 USDT max loss at 6% stop -> 1000 notional
    assert v.approved and v.adjusted_notional == pytest.approx(1000)


def test_min_order_notional(rm):
    v = rm.check(buy(), make_portfolio(cash=5, equity=10000), ctl())
    assert not v.approved and not names(v)["min_order_notional"].passed


def test_zero_equity_blocks(rm):
    v = rm.check(buy(), make_portfolio(cash=0, equity=0), ctl())
    assert not v.approved and "zero" in names(v)["position_size"].detail


def test_confirmation_mode(rm):
    v = rm.check(buy(), make_portfolio(), ctl(confirmation_mode=True))
    assert v.approved and v.requires_confirmation and "awaiting user confirmation" in v.reasons[0]
    rm.set_confirmation_mode(True)
    assert rm.check(buy(), make_portfolio(), ctl()).requires_confirmation  # most restrictive wins
    rm.set_confirmation_mode(False)
    assert not rm.check(buy(), make_portfolio(), ctl()).requires_confirmation


def test_default_manager_requires_confirmation(clock):
    rm = DefaultRiskManager(clock=clock)
    assert rm.check(buy(), make_portfolio(), ControlState()).requires_confirmation


def test_blocked_never_requires_confirmation(rm):
    v = rm.check(buy(), make_portfolio(), ctl(paused=True, confirmation_mode=True))
    assert not v.approved and not v.requires_confirmation


def test_apply_manual_strategy_rules(rm):
    s = ManualStrategy(symbol="TSLAx", activation=ActivationMode.AUTO_EXECUTE,
                       risk=PositionRiskRules(position_size_pct=10, max_open_positions=2, cooldown_bars=2))
    rm.apply_manual_strategy(s)
    p = make_portfolio(cash=9000, positions=[pos()], bars_since_exit={"TSLAx": 3})
    v = rm.check(buy("TSLAx"), p, ctl())
    assert v.approved and v.adjusted_notional == pytest.approx(1000) and not v.requires_confirmation
    # NVDAon still on global defaults (max 1 position)
    assert not rm.check(buy("NVDAon"), p, ctl()).approved
    rm.set_symbol_rules("TSLAx", SymbolRiskRules(activation=ActivationMode.CONFIRMATION))
    assert rm.check(buy("TSLAx"), make_portfolio(), ctl()).requires_confirmation
    rm.set_symbol_rules("TSLAx", None)
    assert "TSLAx" not in rm.config.symbol_rules


# ---------------------------------------------------------------- SELL
def test_sell_requires_position(rm):
    v = rm.check(sell(), make_portfolio(), ctl())
    assert not v.approved and "nothing to sell" in v.reasons[0]


def test_sell_full_position_by_default(rm):
    p = make_portfolio(cash=9000, positions=[pos(qty=10, entry=100)])
    v = rm.check(sell(price=110), p, ctl())
    assert v.approved and v.adjusted_notional == pytest.approx(1100)
    v = rm.check(sell(price=110, proposed_notional=500), p, ctl())
    assert v.adjusted_notional == pytest.approx(500)
    v = rm.check(sell(price=110, proposed_notional=5000), p, ctl())
    assert v.adjusted_notional == pytest.approx(1100)


def test_sell_allowed_while_paused_and_emergency(rm):
    p = make_portfolio(cash=9000, positions=[pos()])
    assert rm.check(sell(), p, ctl(paused=True)).approved
    rm.emergency_stop()
    v = rm.check(sell(), p, ctl(confirmation_mode=True))
    assert v.approved and not v.requires_confirmation


def test_sell_blocked_when_paused_if_configured(clock):
    rm = DefaultRiskManager(config=RiskConfig(allow_exits_when_paused=False), clock=clock)
    assert not rm.check(sell(), make_portfolio(cash=9000, positions=[pos()]), ctl(paused=True)).approved


def test_strategy_sell_needs_confirmation_but_risk_exit_does_not(rm):
    p = make_portfolio(cash=9000, positions=[pos()])
    assert rm.check(sell(), p, ctl(confirmation_mode=True)).requires_confirmation
    exit_sig = rm.check_exits(p, {"NVDAon": 90.0})[0]
    v = rm.check(exit_sig, p, ctl(confirmation_mode=True))
    assert v.approved and not v.requires_confirmation and "Protective" in names(v)["confirmation"].detail


def test_timestamps_from_clock(rm, clock):
    assert rm.check(buy(), make_portfolio(), ctl()).timestamp == clock()


def test_sell_targets_position_id(rm):
    a, b = pos(qty=10, entry=100), pos(qty=5, entry=100)
    p = make_portfolio(cash=8500, positions=[a, b])
    v = rm.check(sell(price=100, position_id=b.id), p, ctl())
    assert v.approved and v.adjusted_notional == pytest.approx(500)
    v = rm.check(sell(price=100), p, ctl())
    assert v.adjusted_notional == pytest.approx(1500)
    v = rm.check(sell(price=100, position_id="gone"), p, ctl())
    assert not v.approved and "nothing to sell" in v.reasons[0]

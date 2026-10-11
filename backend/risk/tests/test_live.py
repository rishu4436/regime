"""LIVE (real funds) profile: hard limits that act autonomously."""
from datetime import datetime, time, timedelta, timezone

import pytest

from backend.contracts import (
    Action, AgentMode, Candle, ControlState, ExecutionPath, ExecutionResult, ExitReason, OrderSide, OrderStatus, Quote,
    Signal, TradingMode,
)
from backend.risk import DefaultRiskManager, LiveContext, LiveRiskProfile, RiskConfig, in_market_hours

from .conftest import T0, make_portfolio, pos

LIVE_CHECKS = {"live_daily_loss_cap", "live_stale_data", "live_market_hours", "live_gas_reserve", "live_order_cap",
               "live_max_exposure", "live_daily_notional", "live_wallet_balance", "live_quote", "live_slippage",
               "live_quote_notional", "live_wallet_quote"}


def names(v):
    return {c.name: c for c in v.checks}


def live_rm(clock, allow_live=True, confirmation=False, **profile):
    return DefaultRiskManager(ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=confirmation),
                              config=RiskConfig(allow_live=allow_live, live=LiveRiskProfile(**profile)), clock=clock)


def lctl(**kw):
    kw.setdefault("confirmation_mode", False)
    return ControlState(trading_mode=TradingMode.LIVE, **kw)


def lpf(cash=200.0, positions=(), **kw):
    return make_portfolio(cash=cash, positions=positions, starting_capital=200.0, mode=TradingMode.LIVE, **kw)


def lbuy(symbol="NVDAon", price=100.0, **kw):
    return Signal(symbol=symbol, action=Action.BUY, source=AgentMode.AUTO, strategy="TREND_FOLLOWING",
                  reason="trend", price=price, **kw)


def quote(notional=25.0, price=100.2, age=5, ttl=30, symbol="NVDAon", side=OrderSide.BUY, slippage_bps=50, fee=0.025):
    return Quote(symbol=symbol, side=side, path=ExecutionPath.SWAP, notional=notional, quantity=notional / price,
                 price=price, fee=fee, slippage_bps=slippage_bps, created_at=T0 - timedelta(seconds=age),
                 expires_at=T0 - timedelta(seconds=age) + timedelta(seconds=ttl))


def ctx(**kw):
    base = dict(data_timestamp=T0 - timedelta(seconds=60), balances={"USDT": 100.0, "BNB": 0.01},
                reference_price=100.0, quote=quote())
    base.update(kw)
    return LiveContext(**base)


def fill(notional, mode=TradingMode.LIVE, side=OrderSide.BUY, status=OrderStatus.FILLED, ts=T0):
    return ExecutionResult(client_order_id="c", symbol="NVDAon", side=side, status=status, path=ExecutionPath.SWAP,
                           mode=mode, notional=notional, timestamp=ts)


# ------------------------------------------------------------------ happy path / autonomy
def test_live_buy_approved_autonomously_within_limits(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx())
    assert v.approved and not v.requires_confirmation, v.reasons
    assert v.adjusted_notional == pytest.approx(25.0)  # 15% of 200 = 30 -> per-order cap 25
    assert LIVE_CHECKS <= set(names(v)) and all(c.passed and c.detail for c in v.checks)
    assert "reduced from 30.00 to 25.00" in names(v)["live_order_cap"].detail


def test_confirmation_only_when_user_turns_it_on(clock):
    rm = live_rm(clock, confirmation=True)
    v = rm.check(lbuy(), lpf(), lctl(confirmation_mode=True), context=ctx())
    assert v.approved and v.requires_confirmation


def test_protective_sell_never_waits_or_blocks_in_live(clock):
    rm = live_rm(clock, allow_live=False, confirmation=True)
    p = lpf(cash=175, positions=[pos(qty=0.25, entry=100)])
    [s] = rm.check_exits(p, {"NVDAon": 90.0})
    v = rm.check(s, p, lctl(confirmation_mode=True))  # no context, LIVE disabled: still closes
    assert v.approved and not v.requires_confirmation


def test_sim_ignores_live_guards(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), clock=clock)
    v = rm.check(lbuy(), make_portfolio(), ControlState(confirmation_mode=False))
    assert v.approved and not (LIVE_CHECKS & set(names(v)))


# ------------------------------------------------------------------ allow_live gate / env switches
def test_allow_live_false_blocks_live_buy(clock):
    v = live_rm(clock, allow_live=False).check(lbuy(), lpf(), lctl(), context=ctx())
    assert not v.approved and "allow_live=False" in names(v)["trading_mode"].detail


def test_portfolio_mode_live_is_enough_to_apply_profile(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(allow_live=True), clock=clock)
    v = rm.check(lbuy(), lpf(), ControlState(confirmation_mode=False), context=LiveContext())
    assert not v.approved and "live_stale_data" in names(v)


@pytest.mark.parametrize("flags,expected", [
    ({}, False),
    ({"RISK_ALLOW_LIVE": "true"}, False),
    ({"RISK_ALLOW_LIVE": "true", "LIVE_TRADING_ENABLED": "true"}, False),
    ({"RISK_ALLOW_LIVE": "true", "LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_ENABLED": "TRUE"}, True),
    ({"RISK_ALLOW_LIVE": "1", "LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_ENABLED": "true"}, False),
])
def test_from_env_requires_all_three_switches(monkeypatch, flags, expected):
    for k in ("RISK_ALLOW_LIVE", "LIVE_TRADING_ENABLED", "BINANCE_LIVE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in flags.items():
        monkeypatch.setenv(k, v)
    assert RiskConfig.from_env().allow_live is expected
    assert DefaultRiskManager().config.allow_live is expected  # default config comes from env


def test_from_env_numeric_overrides(monkeypatch):
    monkeypatch.setenv("RISK_LIVE_MAX_ORDER_USDT", "10")
    monkeypatch.setenv("RISK_LIVE_MAX_DAILY_LOSS_USDT", "7.5")
    monkeypatch.setenv("RISK_LIVE_MARKET_HOURS", "false")
    lp = RiskConfig.from_env().live
    assert lp.max_order_notional_usdt == 10 and lp.max_daily_loss_usdt == 7.5 and not lp.market_hours_enabled
    assert lp.max_daily_notional_usdt == 100


def test_market_hours_env_opt_in(monkeypatch):
    monkeypatch.setenv("RISK_LIVE_MARKET_HOURS", "true")
    assert RiskConfig.from_env().live.market_hours_enabled


def test_live_profile_defaults():
    lp = LiveRiskProfile()
    assert (lp.max_order_notional_usdt, lp.max_daily_loss_usdt, lp.max_daily_notional_usdt, lp.max_open_exposure_usdt,
            lp.max_data_age_seconds, lp.max_slippage_pct, lp.fee_buffer_usdt, lp.min_gas_reserve) == (
        25, 25, 100, 50, 900, 1.0, 1.0, 0.002)
    assert not lp.market_hours_enabled and lp.market_open_utc == time(13, 30) and lp.market_close_utc == time(20, 0)
    assert lp.require_wallet_balances and not lp.require_quote
    assert not hasattr(lp, "allow_unconfirmed_live")


# ------------------------------------------------------------------ size caps
def test_per_order_cap_configurable(clock):
    v = live_rm(clock, max_order_notional_usdt=12).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(notional=12)))
    assert v.approved and v.adjusted_notional == pytest.approx(12)


def test_open_exposure_cap(clock):
    rm = live_rm(clock)
    cfg = rm.config.model_copy(update={"position_rules": rm.config.position_rules.model_copy(update={"max_open_positions": 3})})
    rm.set_config(cfg)
    p = lpf(cash=160, positions=[pos("TSLAx", qty=0.2, entry=200)])  # 40 USDT open
    v = rm.check(lbuy(), p, lctl(), context=ctx(quote=quote(notional=10)))
    assert v.approved and v.adjusted_notional == pytest.approx(10) and "reduced" in names(v)["live_max_exposure"].detail
    p = lpf(cash=155, positions=[pos("TSLAx", qty=0.225, entry=200)])  # 45 open -> 5 left < 10 minimum
    v = rm.check(lbuy(), p, lctl(), context=ctx(quote=quote(notional=5)))
    assert not v.approved and not names(v)["min_order_notional"].passed


def test_daily_notional_from_context(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(daily_live_notional_usdt=85, quote=quote(notional=15)))
    assert v.approved and v.adjusted_notional == pytest.approx(15)
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(daily_live_notional_usdt=95))
    assert not v.approved and "reduced from 25.00 to 5.00" in names(v)["live_daily_notional"].detail


def test_daily_notional_ledger_from_record_execution(clock):
    rm = live_rm(clock)
    for r in (fill(25), fill(25), fill(25), fill(25, mode=TradingMode.SIM), fill(25, side=OrderSide.SELL),
              fill(25, status=OrderStatus.FAILED), fill(25, ts=T0 - timedelta(days=1))):
        rm.record_execution(r)
    assert rm.daily_live_notional_used(now=T0) == 75
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(25)
    rm.record_execution(fill(20))
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx())
    assert not v.approved  # 95 used -> 5 left < 10 minimum
    # context and ledger: the larger wins
    assert rm.daily_live_notional_used(LiveContext(now=T0, daily_live_notional_usdt=99)) == 99
    clock.advance(days=1)
    assert rm.daily_live_notional_used() == 0


def test_wallet_balance_caps_and_gas_reserve(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(balances={"usdt": 20, "bnb": 0.01}, quote=quote(notional=19, fee=0)))
    assert v.approved and v.adjusted_notional == pytest.approx(19)  # 20 - 1 fee buffer
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(balances={"USDT": 5, "BNB": 0.01}))
    assert not v.approved
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(balances={"USDT": 100, "BNB": 0.001}))
    assert not v.approved and "not enough gas" in names(v)["live_gas_reserve"].detail
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(balances=None))
    assert not v.approved and "cannot verify" in names(v)["live_gas_reserve"].detail
    rm2 = live_rm(clock, require_wallet_balances=False)
    assert rm2.check(lbuy(), lpf(), lctl(), context=ctx(balances=None)).approved


def test_quote_plus_fee_must_fit_wallet(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy(), lpf(), lctl(), context=ctx(balances={"USDT": 26.0, "BNB": 0.01}, quote=quote(notional=25, fee=0.5)))
    assert not v.approved and not names(v)["live_wallet_quote"].passed


# ------------------------------------------------------------------ daily loss cap -> emergency stop
def test_daily_loss_cap_triggers_emergency_stop(clock):
    rm = live_rm(clock)
    p = lpf(cash=170, positions=[pos(qty=0.3, entry=100, current_price=80)], daily_pnl=-20.0)  # -20 realized -6 unrealized
    assert rm.daily_loss_usdt(p) == pytest.approx(26)
    v = rm.check(lbuy(), p, lctl(), context=ctx())
    assert not v.approved and not names(v)["live_daily_loss_cap"].passed and not names(v)["emergency_stop"].passed
    s = rm.get_control_state()
    assert s.emergency_stopped and "daily loss 26.00 USDT" in s.reason and "25.00" in s.reason
    sigs = rm.check_exits(p, {"NVDAon": 80.0})
    assert [x.exit_reason for x in sigs] == [ExitReason.EMERGENCY_STOP]
    assert rm.resume().emergency_stopped  # only reset_emergency_stop clears it
    assert not rm.reset_emergency_stop().emergency_stopped


def test_daily_loss_cap_triggered_from_check_exits_alone(clock):
    rm = live_rm(clock, max_daily_loss_usdt=5)
    p = lpf(cash=170, positions=[pos(qty=0.3, entry=100)], daily_pnl=-6)
    sigs = rm.check_exits(p, {"NVDAon": 100.0})
    assert rm.get_control_state().emergency_stopped and sigs[0].exit_reason == ExitReason.EMERGENCY_STOP


def test_daily_loss_below_cap_and_sim_never_triggers(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy(), lpf(daily_pnl=-24.9), lctl(), context=ctx())
    assert names(v)["live_daily_loss_cap"].passed and not rm.get_control_state().emergency_stopped
    assert not names(v)["daily_loss_limit"].passed  # the % limit (5% of 200 = 10) still blocks new entries
    sim = DefaultRiskManager(clock=clock)
    sim.check(lbuy(), make_portfolio(daily_pnl=-5000), ControlState())
    assert not sim.get_control_state().emergency_stopped


def test_unrealized_falls_back_to_position_field(clock):
    rm = live_rm(clock)
    p = lpf(cash=170, positions=[pos(qty=0.3, entry=100, unrealized_pnl=-3.0)], daily_pnl=-1)
    assert rm.daily_loss_usdt(p) == pytest.approx(4)
    assert rm.daily_loss_usdt(lpf(daily_pnl=10)) == 0


# ------------------------------------------------------------------ stale data
@pytest.mark.parametrize("age,ok", [(0, True), (899, True), (900, True), (901, False), (-30, True), (-120, False)])
def test_stale_data_guard(clock, age, ok):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(data_timestamp=T0 - timedelta(seconds=age)))
    assert names(v)["live_stale_data"].passed is ok and v.approved is ok


def test_stale_data_missing_timestamp_blocks(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(data_timestamp=None))
    assert not v.approved and "No price/bar timestamp" in names(v)["live_stale_data"].detail
    v = live_rm(clock).check(lbuy(), lpf(), lctl())  # no context at all
    assert not v.approved


def test_stale_data_from_bar_close_time(clock):
    bar = Candle(timestamp=T0 - timedelta(hours=1), open=100, high=101, low=99, close=100, volume=1)
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(data_timestamp=None, bar=bar, bar_interval=timedelta(hours=1)))
    assert v.approved
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(data_timestamp=None, bar=bar))  # open time only: 3600s old
    assert not v.approved


def test_naive_timestamps_treated_as_utc(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(data_timestamp=(T0 - timedelta(seconds=10)).replace(tzinfo=None)))
    assert v.approved


# ------------------------------------------------------------------ market hours
@pytest.mark.parametrize("now,ok", [
    (datetime(2026, 1, 5, 13, 29, tzinfo=timezone.utc), False),
    (datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc), True),
    (datetime(2026, 1, 5, 19, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc), False),  # Saturday
    (datetime(2026, 1, 5, 20, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))), True),  # 15:00 UTC
])
def test_market_hours(now, ok):
    assert in_market_hours(now, LiveRiskProfile())[0] is ok


def test_market_hours_guard_in_check(clock):
    sat = T0 + timedelta(days=5)
    c = ctx(now=sat, data_timestamp=sat, quote=None)
    assert live_rm(clock).check(lbuy(), lpf(), lctl(), context=c).approved  # 24/7 by default
    v = live_rm(clock, market_hours_enabled=True).check(lbuy(), lpf(), lctl(), context=c)
    assert not v.approved and "weekend" in names(v)["live_market_hours"].detail
    assert live_rm(clock, market_hours_enabled=True, market_hours_exempt_symbols=["NVDAon"]).check(lbuy(), lpf(), lctl(), context=c).approved


# ------------------------------------------------------------------ quote guards
def test_quote_expired_or_too_old(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(age=31)))
    assert not v.approved and "expired" in names(v)["live_quote"].detail
    v = live_rm(clock, max_quote_age_seconds=10).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(age=15)))
    assert not v.approved and "15s old" in names(v)["live_quote"].detail


def test_quote_mismatch(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(symbol="TSLAx")))
    assert not v.approved
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(side=OrderSide.SELL)))
    assert not v.approved


@pytest.mark.parametrize("qprice,bps,ok", [(100.99, 50, True), (101.5, 50, False), (98.0, 50, False), (100.2, 150, False)])
def test_quote_slippage(clock, qprice, bps, ok):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(price=qprice, slippage_bps=bps)))
    assert names(v)["live_slippage"].passed is ok and v.approved is ok


def test_slippage_reference_fallbacks(clock):
    rm = live_rm(clock)
    assert rm.check(lbuy(price=100.0), lpf(), lctl(), context=ctx(reference_price=None)).approved  # signal.price
    v = rm.check(lbuy(price=None), lpf(), lctl(), context=ctx(reference_price=None))
    assert not v.approved and "No reference price" in names(v)["live_slippage"].detail


def test_quote_notional_above_approved_size_blocked(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=quote(notional=30)))
    assert not v.approved and "re-quote" in names(v)["live_quote_notional"].detail


def test_quote_optional_unless_required(clock):
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(quote=None))
    assert v.approved and "re-checks with the quote" in names(v)["live_quote"].detail
    v = live_rm(clock, require_quote=True).check(lbuy(), lpf(), lctl(), context=ctx(quote=None))
    assert not v.approved


# ------------------------------------------------------------------ UI limits block
def test_live_limits_dict(clock):
    rm = live_rm(clock)
    rm.record_execution(fill(30))
    p = lpf(cash=170, positions=[pos(qty=0.3, entry=100, current_price=90)], daily_pnl=-2)
    d = rm.live_limits(p)
    assert {k: d[k] for k in ("per_order_cap_usdt", "daily_loss_cap_usdt", "daily_notional_cap_usdt", "max_exposure_usdt",
                              "max_slippage_pct", "stale_after_s", "bnb_gas_reserve")} == {
        "per_order_cap_usdt": 25, "daily_loss_cap_usdt": 25, "daily_notional_cap_usdt": 100, "max_exposure_usdt": 50,
        "max_slippage_pct": 1.0, "stale_after_s": 900, "bnb_gas_reserve": 0.002}
    assert d["daily_used_usdt"] == 30 and d["daily_loss_usdt"] == pytest.approx(5) and d["open_exposure_usdt"] == pytest.approx(27)
    assert d["live_allowed"] and d["market_hours_utc"] is None
    assert live_rm(clock, market_hours_enabled=True).live_limits()["market_hours_utc"] == "13:30-20:00 Mon-Fri"
    bare = DefaultRiskManager(config=RiskConfig()).live_limits()
    assert bare["daily_loss_usdt"] is None and bare["daily_used_usdt"] == 0 and not bare["live_allowed"]

"""Runtime-editable LIVE trading limits (dashboard 'Trading limits' panel) and user order sizing."""
import math
import threading

import pytest

from backend.contracts import TradingMode
from backend.risk import DefaultRiskManager, LiveRiskProfile, RiskConfig
from backend.risk.live import WalletLimits

from .conftest import make_portfolio
from .test_live import ctx, lbuy, lctl, live_rm, names

PANEL_KEYS = {"order_mode", "order_usdt", "order_pct", "max_order_usdt", "max_total_usdt", "max_per_token_usdt",
              "daily_loss_usdt", "daily_buy_usdt"}
RAISED = dict(max_order_usdt=500, max_total_usdt=2000, max_per_token_usdt=1000)


def big_pf(cash=10_000.0, **kw):
    return make_portfolio(cash=cash, starting_capital=10_000.0, mode=TradingMode.LIVE, **kw)


def big_ctx(usdt=10_000.0, **kw):
    kw.setdefault("quote", None)
    return ctx(balances={"USDT": usdt, "BNB": 0.01}, **kw)


# ------------------------------------------------------------------ shape / defaults
def test_defaults_unchanged_and_settings_shape(clock):
    lp = LiveRiskProfile()
    assert (lp.max_order_notional_usdt, lp.max_daily_loss_usdt, lp.max_daily_notional_usdt, lp.max_open_exposure_usdt) == (25, 25, 100, 50)
    assert lp.order_mode == "fixed" and lp.order_usdt is None and lp.order_pct is None and lp.max_token_exposure_usdt is None
    s = live_rm(clock).live_limits_settings()
    assert PANEL_KEYS | {"defaults", "wallet_daily_limit_usdt", "notes"} == set(s)
    assert {k: s[k] for k in PANEL_KEYS} == s["defaults"] == dict(
        order_mode="fixed", order_usdt=None, order_pct=None, max_order_usdt=25.0, max_total_usdt=50.0,
        max_per_token_usdt=50.0, daily_loss_usdt=25.0, daily_buy_usdt=100.0)
    assert s["wallet_daily_limit_usdt"] is None and s["notes"] == []
    assert live_rm(clock).live_limits_settings(WalletLimits(daily_limit_usdt=300))["wallet_daily_limit_usdt"] == 300


def test_default_check_lists_unchanged(clock):
    v = live_rm(clock).check(lbuy(), make_portfolio(cash=200, starting_capital=200, mode=TradingMode.LIVE), lctl(), context=ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(25.0)
    assert "live_token_exposure" not in names(v) and "reserved_orders" not in names(v)
    assert names(v)["position_size"].detail.startswith("rule 15")


# ------------------------------------------------------------------ set / validation / atomic / reset
def test_set_applies_and_live_limits_reflects(clock):
    rm = live_rm(clock)
    out = rm.set_live_limits(dict(order_mode="pct", order_pct=2, max_order_usdt=200, max_total_usdt=1000,
                                  max_per_token_usdt=400, daily_loss_usdt=100, daily_buy_usdt=800))
    assert out["max_order_usdt"] == 200 and out["daily_buy_usdt"] == 800 and out["notes"] == []
    lp = rm.config.live
    assert (lp.max_order_notional_usdt, lp.max_open_exposure_usdt, lp.max_token_exposure_usdt, lp.max_daily_loss_usdt,
            lp.max_daily_notional_usdt, lp.order_mode, lp.order_pct) == (200, 1000, 400, 100, 800, "pct", 2)
    ll = rm.live_limits()
    assert ll["per_order_cap_usdt"] == 200 and ll["max_exposure_usdt"] == 1000 and ll["daily_loss_cap_usdt"] == 100
    assert ll["max_token_exposure_usdt"] == 400 and ll["order_mode"] == "pct" and ll["order_pct"] == 2
    assert out["defaults"]["max_order_usdt"] == 25  # defaults stay the startup values


def test_partial_update_keeps_other_values_and_echoed_read_only_keys_ignored(clock):
    rm = live_rm(clock)
    s = rm.live_limits_settings()
    s["daily_loss_usdt"] = 40
    out = rm.set_live_limits(s)  # GET body echoed back with one change
    assert out["daily_loss_usdt"] == 40 and out["max_order_usdt"] == 25
    out = rm.set_live_limits({"max_total_usdt": 80})
    assert out["max_total_usdt"] == 80 and out["daily_loss_usdt"] == 40


def test_daily_buy_auto_raised_when_max_order_exceeds_it(clock):
    rm = live_rm(clock)
    out = rm.set_live_limits(dict(max_order_usdt=200, max_total_usdt=1000))
    assert out["daily_buy_usdt"] == 800 and "raised from 100.00 to 800.00" in out["notes"][0]
    out = rm.set_live_limits(dict(max_order_usdt=150))  # below the current daily: unchanged
    assert out["daily_buy_usdt"] == 800 and out["notes"] == []


@pytest.mark.parametrize("bad, msg", [
    (dict(max_order_usdt=0), "Max per order must be greater than 0"),
    (dict(max_order_usdt=-5), "greater than 0"),
    (dict(max_total_usdt=math.inf), "finite"),
    (dict(daily_loss_usdt=math.nan), "finite"),
    (dict(daily_loss_usdt=None), "Daily loss limit is required"),
    (dict(max_order_usdt="abc"), "must be a number"),
    (dict(max_order_usdt=True), "not true/false"),
    (dict(order_mode="pct", order_pct=0), "greater than 0"),
    (dict(order_mode="pct", order_pct=150), "at most 100"),
    (dict(order_mode="pct"), "required when the order size mode is 'pct'"),
    (dict(order_mode="kelly"), "'fixed' or 'pct'"),
    (dict(max_order_usdt=60), "Max per order (60.00 USDT) can't be above Max total invested (50.00 USDT)"),
    (dict(max_per_token_usdt=60), "Max in one token (60.00 USDT) can't be above Max total invested"),
    (dict(order_usdt=30), "Order size (30.00 USDT) can't be above Max per order (25.00 USDT)"),
    (dict(daily_buy_usdt=20), "Daily buy limit (20.00 USDT) can't be below Max per order"),
    (dict(max_leverage=3), "Unknown setting(s): max_leverage"),
])
def test_validation_errors(clock, bad, msg):
    rm = live_rm(clock)
    before = rm.config.live
    with pytest.raises(ValueError, match=None) as e:
        rm.set_live_limits(bad)
    assert msg in str(e.value)
    assert rm.config.live == before


def test_wallet_daily_limit_is_the_ceiling(clock):
    rm = live_rm(clock)
    wl = WalletLimits(daily_limit_usdt=100)
    with pytest.raises(ValueError, match="above the wallet's own daily limit of 100.00 USDT"):
        rm.set_live_limits(dict(max_order_usdt=200, max_total_usdt=1000), wallet_limits=wl)
    out = rm.set_live_limits(dict(max_order_usdt=100, max_total_usdt=1000, daily_buy_usdt=500), wallet_limits=wl)
    assert out["wallet_daily_limit_usdt"] == 100 and "wallet limit (the lower one) applies" in out["notes"][0]


def test_invalid_request_is_all_or_nothing(clock):
    rm = live_rm(clock)
    before = rm.live_limits_settings()
    with pytest.raises(ValueError) as e:  # one good value + two bad: nothing applied, both reported
        rm.set_live_limits(dict(daily_loss_usdt=80, max_order_usdt=-1, max_total_usdt="x"))
    assert "Max per order" in str(e.value) and "Max total invested" in str(e.value)
    assert rm.live_limits_settings() == before


def test_reset_restores_startup_values(clock):
    rm = live_rm(clock, max_order_notional_usdt=20.0)  # e.g. RISK_LIVE_MAX_ORDER_USDT=20 at startup
    rm.set_live_limits(dict(order_mode="pct", order_pct=2, **RAISED, daily_loss_usdt=300))
    out = rm.reset_live_limits()
    assert {k: out[k] for k in PANEL_KEYS} == out["defaults"]
    assert out["max_order_usdt"] == 20 and out["order_mode"] == "fixed" and out["order_pct"] is None
    assert rm.config.live.max_token_exposure_usdt is None and out["notes"]


# ------------------------------------------------------------------ order sizing
def test_pct_sizing_clamped_by_default_caps(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(order_mode="pct", order_pct=2))
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(25.0)
    assert "2% of wallet USDT 10,000.00 = 200.00 USDT" in names(v)["position_size"].detail
    assert "reduced from 200.00 to 25.00" in names(v)["live_order_cap"].detail


def test_pct_sizing_10k_wallet_2pct_is_200_when_caps_raised(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(order_mode="pct", order_pct=2, **RAISED))
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(200.0), v.reasons


def test_pct_sizing_still_clamped_by_wallet_balance_quota_and_exit_reserve(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(order_mode="pct", order_pct=50, **RAISED))
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx(usdt=300))  # 50% of 300 = 150, fits balance
    assert v.adjusted_notional == pytest.approx(150.0)
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx(wallet_limits=WalletLimits(daily_limit_usdt=200, daily_used_usdt=0)))
    assert v.adjusted_notional == pytest.approx(100.0)  # exit reserve: (200 - 0) / 2
    assert "reduced" in names(v)["wallet_exit_reserve"].detail


def test_pct_sizing_bump_to_min_order_still_applies(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(order_mode="pct", order_pct=0.05))  # 0.05% of 10,000 = 5 < 10 minimum
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(10.0)
    assert "raised from 5.00 to 10.00" in names(v)["min_order_notional"].detail


def test_fixed_sizing(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(order_mode="fixed", order_usdt=40, max_order_usdt=100, max_total_usdt=200))
    v = rm.check(lbuy(), big_pf(), lctl(), context=big_ctx())
    assert v.approved and v.adjusted_notional == pytest.approx(40.0)
    assert "order size setting 40.00 USDT per order" in names(v)["position_size"].detail


def test_fixed_without_order_usdt_keeps_strategy_size(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(max_order_usdt=100, max_total_usdt=200))
    v = rm.check(lbuy(proposed_notional=60), big_pf(), lctl(), context=big_ctx())
    assert v.adjusted_notional == pytest.approx(60.0)
    assert names(v)["position_size"].detail.startswith("signal asked 60.00 USDT")


def test_order_size_setting_ignored_in_sim(clock):
    from .conftest import buy, ctl
    rm = DefaultRiskManager(ctl(), config=RiskConfig(), clock=clock)
    rm.set_live_limits(dict(order_mode="pct", order_pct=2, **RAISED))
    v = rm.check(buy(), make_portfolio(cash=10_000), ctl())
    assert v.adjusted_notional == pytest.approx(1500.0)  # rule 15% of equity, as before


def test_live_per_token_cap(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(max_order_usdt=100, max_total_usdt=300, max_per_token_usdt=120, order_usdt=100))
    from .conftest import pos
    pf = big_pf(cash=9_900, positions=[pos(symbol="NVDAon", qty=1, entry=100)])
    v = rm.check(lbuy("NVDAon"), pf, lctl(), context=big_ctx())
    assert v.adjusted_notional == pytest.approx(20.0)  # 120 - 100 held
    v = rm.check(lbuy("AAPLon"), pf, lctl(), context=big_ctx())
    assert v.adjusted_notional == pytest.approx(100.0)


# ------------------------------------------------------------------ thread safety
def test_concurrent_set_and_check_see_consistent_snapshots(clock):
    rm = live_rm(clock)
    low = dict(order_mode="pct", order_pct=2, max_order_usdt=25, max_total_usdt=50, max_per_token_usdt=50)
    high = dict(order_mode="pct", order_pct=2, **RAISED)
    errors, sizes = [], []

    def writer():
        try:
            for i in range(200):
                rm.set_live_limits(high if i % 2 else low)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(200):
                sizes.append(round(rm.check(lbuy(), big_pf(), lctl(), context=big_ctx()).adjusted_notional, 6))
                s = rm.live_limits_settings()
                assert s["max_per_token_usdt"] <= s["max_total_usdt"]
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert set(sizes) <= {25.0, 200.0}

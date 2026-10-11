"""bot3/live-fixes: LIVE min-order bump, default max_open_positions = 3, %-daily-loss skipped in LIVE, min_wallet_usdt."""
from datetime import timedelta

import pytest

from backend.contracts import Action, ActivationMode, ControlState, ManualStrategy, PositionRiskRules, TradingMode
from backend.risk import DefaultRiskManager, LiveContext, LiveRiskProfile, RiskConfig, SymbolRiskRules
from backend.risk.live import TokenStatus, WalletLimits

from .conftest import T0, make_portfolio, pos
from .test_live import lbuy, lctl, live_rm, names, quote


def wallet(W, positions=(), **kw):
    return make_portfolio(cash=W - sum(p.quantity * p.avg_entry_price for p in positions), positions=positions,
                          starting_capital=W, mode=TradingMode.LIVE, **kw)


def wctx(W, tmin=None, **kw):
    base = dict(data_timestamp=T0 - timedelta(seconds=60), balances={"USDT": W, "BNB": 0.01}, reference_price=100.0,
                token_status=TokenStatus(trading_status="trading", min_order_usdt=tmin))
    base.update(kw)
    return LiveContext(**base)


# ------------------------------------------------------------------ 1. bump to minimum in LIVE
def test_bump_fits_and_is_approved(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100))
    assert v.approved, v.reasons
    assert v.adjusted_notional == pytest.approx(10.0)
    d = names(v)["min_order_notional"].detail
    assert "raised from 2.00 to 10.00 USDT to meet the minimum order" in d
    assert any("raised from" in r for r in v.reasons)


def test_bump_with_quote_second_pass_is_approved(clock):
    rm = live_rm(clock)
    q = quote(notional=10.0, symbol="NVDAB")
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100, quote=q))
    assert v.approved and v.adjusted_notional == pytest.approx(10.0), v.reasons
    assert names(v)["live_quote_notional"].passed and names(v)["live_wallet_quote"].passed


def test_ondo_20_bump_on_100_wallet_approved_at_20(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAon", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100, tmin=20.0))
    assert v.approved and v.adjusted_notional == pytest.approx(20.0), v.reasons
    assert "to meet the NVDAon token minimum" in names(v)["min_order_notional"].detail


def test_30_wallet_nvdab_bumped_to_10(clock):
    # 30 - 1 fee buffer = 29 >= 10; the % rules (15% = 4.50, token 30% = 9) are overridden, hard caps are not
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAB", proposed_notional=0.6), wallet(30), lctl(), context=wctx(30))
    assert v.approved and v.adjusted_notional == pytest.approx(10.0), v.reasons
    assert names(v)["live_wallet_balance"].passed


def test_bump_blocked_when_balance_too_small(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), wallet(10.5), lctl(), context=wctx(10.5))
    assert not v.approved and v.adjusted_notional == 0
    d = names(v)["min_order_notional"].detail
    assert "cannot raise to 10.00 USDT" in d and "at most 9.50 USDT" in d


def test_bump_blocked_when_token_min_above_per_order_cap(clock):
    rm = live_rm(clock, max_order_notional_usdt=15.0)
    v = rm.check(lbuy("NVDAon", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100, tmin=20.0))
    assert not v.approved and "cannot raise to 20.00 USDT" in names(v)["min_order_notional"].detail


@pytest.mark.parametrize("ctx_kw,profile", [
    (dict(wallet_limits=WalletLimits(per_tx_limit_usdt=8.0)), {}),                     # wallet per-tx cap
    (dict(daily_live_notional_usdt=95.0), {}),                                          # our daily notional room 5
    (dict(wallet_limits=WalletLimits(daily_limit_usdt=15.0)), {}),                      # exit reserve (15-0)/2 = 7.5
    ({}, dict(max_open_exposure_usdt=8.0)),                                             # LIVE exposure cap
])
def test_bump_never_exceeds_hard_caps(clock, ctx_kw, profile):
    rm = live_rm(clock, **profile)
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100, **ctx_kw))
    assert not v.approved and "cannot raise" in names(v)["min_order_notional"].detail


def test_bump_respects_open_exposure_room(clock):
    rm = live_rm(clock)  # exposure cap 50, 45 open -> room 5 < 10
    p = wallet(200, positions=[pos("AAPLon", qty=0.45, entry=100)])
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), p, lctl(), context=wctx(200))
    assert not v.approved and not names(v)["live_max_exposure"].passed   # the hard cap is the reported reason
    assert "cannot raise" in names(v)["min_order_notional"].detail
    assert v.reasons == [r for r in v.reasons if r.startswith("Blocked (live_max_exposure): LIVE exposure limit reached")]


def test_bump_can_be_turned_off(clock):
    rm = live_rm(clock, bump_to_min_order=False)
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), wallet(100), lctl(), context=wctx(100))
    assert not v.approved and "too small" in names(v)["min_order_notional"].detail
    assert "raise" not in names(v)["min_order_notional"].detail


def test_no_bump_in_sim(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    p = make_portfolio(cash=30, starting_capital=30)
    v = rm.check(lbuy("NVDAB", proposed_notional=2.0), p, ControlState(confirmation_mode=False))
    assert not v.approved and "too small" in names(v)["min_order_notional"].detail


def test_order_above_minimum_unchanged(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAB", proposed_notional=12.0), wallet(100), lctl(), context=wctx(100))
    assert v.approved and v.adjusted_notional == pytest.approx(12.0)
    assert "raised" not in names(v)["min_order_notional"].detail


# ------------------------------------------------------------------ 2. default max_open_positions = 3
def test_default_max_open_positions_is_3(clock):
    cfg = RiskConfig()
    assert cfg.default_max_open_positions == 3
    assert cfg.rules_for("ANY")[0].max_open_positions == 3


def test_multiple_positions_up_to_3(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    c = ControlState(confirmation_mode=False)
    held = []
    for sym in ("AAPLon", "MSFTon", "TSLAon"):
        v = rm.check(lbuy(sym), make_portfolio(cash=10000 - 1000 * len(held), positions=held), c)
        assert v.approved, v.reasons
        held.append(pos(sym, qty=10, entry=100))
    v = rm.check(lbuy("NVDAon"), make_portfolio(cash=7000, positions=held), c)
    assert not v.approved and "3 open position(s), limit 3 - limit reached" in names(v)["max_open_positions"].detail


def test_explicit_max_open_positions_wins(clock):
    cfg = RiskConfig(position_rules=PositionRiskRules(max_open_positions=1), default_max_open_positions=3)
    assert cfg.rules_for("X")[0].max_open_positions == 1
    rm = DefaultRiskManager(config=RiskConfig(), clock=clock)
    rm.apply_manual_strategy(ManualStrategy(symbol="TSLAon", risk=PositionRiskRules(position_size_pct=10)))
    assert rm.config.rules_for("TSLAon")[0].max_open_positions == 3        # not set by the strategy -> risk default
    assert rm.config.rules_for("TSLAon")[0].position_size_pct == 10
    rm.apply_manual_strategy(ManualStrategy(symbol="TSLAon", risk=PositionRiskRules(max_open_positions=2)))
    assert rm.config.rules_for("TSLAon")[0].max_open_positions == 2        # explicit -> kept
    s = ManualStrategy.model_validate({"symbol": "AAPLon", "risk": {"max_open_positions": 1}})
    rm.apply_manual_strategy(s)
    assert rm.config.rules_for("AAPLon")[0].max_open_positions == 1        # explicit in JSON -> kept


# ------------------------------------------------------------------ 3. %-of-capital daily loss rule
def test_pct_daily_loss_skipped_in_live(clock):
    rm = live_rm(clock)
    v = rm.check(lbuy("NVDAB", proposed_notional=10.0), wallet(100, daily_pnl=-8.0), lctl(), context=wctx(100))
    c = names(v)["daily_loss_limit"]   # 5% of 100 = 5 would block; LIVE cap 25 governs
    assert c.passed and c.detail.startswith("LIVE uses the daily loss cap of 25.00 USDT instead")
    assert v.approved, v.reasons


def test_pct_daily_loss_enforced_in_sim_and_paper(clock):
    for mode in (TradingMode.SIM, TradingMode.PAPER):
        c = ControlState(confirmation_mode=False, trading_mode=mode)
        rm = DefaultRiskManager(c, config=RiskConfig(), clock=clock)
        v = rm.check(lbuy("NVDAon"), make_portfolio(cash=10000, daily_pnl=-600, mode=mode), c)
        assert not v.approved and not names(v)["daily_loss_limit"].passed
        assert "daily loss limit hit" in names(v)["daily_loss_limit"].detail


def test_pct_daily_loss_in_live_when_flag_on(clock):
    rm = live_rm(clock, use_pct_daily_loss_in_live=True)
    v = rm.check(lbuy("NVDAB", proposed_notional=10.0), wallet(100, daily_pnl=-8.0), lctl(), context=wctx(100))
    assert not v.approved and not names(v)["daily_loss_limit"].passed


# ------------------------------------------------------------------ 4. min_wallet_usdt in live_limits()
def test_min_wallet_usdt_in_live_limits(clock):
    rm = live_rm(clock)
    lim = rm.live_limits()
    assert lim["min_order_usdt"] == 10.0 and lim["min_wallet_usdt"] == pytest.approx(11.0)
    assert "token minimum" not in lim["min_wallet_usdt_note"]
    lim = rm.live_limits(context=wctx(100, tmin=20.0))
    assert lim["min_wallet_usdt"] == pytest.approx(21.0) and "token minimum" in lim["min_wallet_usdt_note"]
    assert not any(w["code"] == "wallet_too_small" for w in lim["warnings"])
    lim = rm.live_limits(context=wctx(15, tmin=20.0))
    assert any(w["code"] == "wallet_too_small" for w in lim["warnings"])


def test_min_wallet_none_when_min_above_per_order_cap(clock):
    rm = live_rm(clock)
    lim = rm.live_limits(context=wctx(100, tmin=20.0, wallet_limits=WalletLimits(per_tx_limit_usdt=15.0)))
    assert lim["min_wallet_usdt"] is None and "above the per-order cap of 15.00 USDT" in lim["min_wallet_usdt_note"]
    assert any(w["code"] == "token_min_above_cap" for w in lim["warnings"])


def test_min_wallet_matches_check(clock):
    """A wallet exactly at min_wallet_usdt gets a minimum-size BUY approved; one cent less is blocked."""
    rm = live_rm(clock)
    mw = rm.live_limits(context=wctx(100, tmin=20.0))["min_wallet_usdt"]
    ok = rm.check(lbuy("NVDAon", proposed_notional=1.0), wallet(mw), lctl(), context=wctx(mw, tmin=20.0))
    assert ok.approved and ok.adjusted_notional == pytest.approx(20.0), ok.reasons
    bad = rm.check(lbuy("NVDAon", proposed_notional=1.0), wallet(mw - 0.01), lctl(), context=wctx(mw - 0.01, tmin=20.0))
    assert not bad.approved


# ------------------------------------------------------------------ 5. a cap that cut the order below the minimum is the reason
def test_token_exposure_used_up_reports_exposure_not_min_order(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    p = make_portfolio(cash=7000, positions=[pos("NVDAB", qty=30, entry=100)])  # 3000 of 30% * 10000 used
    v = rm.check(lbuy("NVDAB"), p, ControlState(confirmation_mode=False))
    assert not v.approved and [c.name for c in v.checks if not c.passed] == ["max_token_exposure"]
    assert v.reasons == [r for r in v.reasons if "NVDAB exposure limit reached: 3,000.00 of 3,000.00 USDT used" in r]
    assert "blocked by max_token_exposure" in names(v)["min_order_notional"].detail


def test_total_exposure_used_up_reports_total_exposure(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    held = [pos("AAPLon", qty=29.95, entry=100), pos("MSFTon", qty=30, entry=100)]
    p = make_portfolio(cash=4005, positions=held)  # 5995 of 60% * 10000 = 6000 used -> 5 left < 10
    v = rm.check(lbuy("NVDAB"), p, ControlState(confirmation_mode=False))
    assert not v.approved and [c.name for c in v.checks if not c.passed] == ["max_total_exposure"]
    assert "Total exposure limit reached: 5,995.00 of 6,000.00 USDT used" in v.reasons[0]


def test_partial_cut_still_above_min_is_approved(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    p = make_portfolio(cash=7500, positions=[pos("NVDAB", qty=25, entry=100)])
    v = rm.check(lbuy("NVDAB"), p, ControlState(confirmation_mode=False))
    assert v.approved and v.adjusted_notional == pytest.approx(500) and all(c.passed for c in v.checks)


# ------------------------------------------------------------------ 6. global confirmation toggle takes priority
@pytest.mark.parametrize("activation", [ActivationMode.CONFIRMATION, ActivationMode.AUTO_EXECUTE, None])
def test_global_confirmation_toggle_wins_over_activation(clock, activation):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    rm.set_symbol_rules("NVDAon", SymbolRiskRules(activation=activation))
    off = rm.check(lbuy(), make_portfolio(), ControlState(confirmation_mode=False))
    assert off.approved and not off.requires_confirmation
    on = rm.check(lbuy(), make_portfolio(), ControlState(confirmation_mode=True))
    assert on.approved and on.requires_confirmation


def test_global_confirmation_toggle_for_strategy_sells(clock):
    rm = DefaultRiskManager(ControlState(confirmation_mode=False), config=RiskConfig(), clock=clock)
    rm.set_symbol_rules("NVDAon", SymbolRiskRules(activation=ActivationMode.CONFIRMATION))
    p = make_portfolio(cash=9000, positions=[pos()])
    s = lbuy().model_copy(update={"action": Action.SELL})
    assert not rm.check(s, p, ControlState(confirmation_mode=False)).requires_confirmation
    assert rm.check(s, p, ControlState(confirmation_mode=True)).requires_confirmation


def test_live_confirmation_follows_global_toggle(clock):
    rm = live_rm(clock)
    rm.set_symbol_rules("NVDAB", SymbolRiskRules(activation=ActivationMode.CONFIRMATION))
    v = rm.check(lbuy("NVDAB", proposed_notional=10.0), wallet(100), lctl(), context=wctx(100))
    assert v.approved and not v.requires_confirmation

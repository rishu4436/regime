"""Wallet (Agentic Wallet / baw) limits, token status, token minimum order and blocked symbol suffixes in LIVE."""
from backend.contracts import Action, AgentMode, ExitReason, Signal
from backend.risk import LiveContext, TokenStatus, WalletLimits

from .conftest import T0, pos
from .test_live import ctx, lbuy, lctl, live_rm, lpf, names, quote


def wl(**kw):
    return WalletLimits(source="baw", **kw)


def check(rm, sig=None, pf=None, **ckw):
    return rm.check(sig or lbuy(), pf or lpf(), lctl(), context=ctx(**ckw))


# ------------------------------------------------------------------ stricter limit wins
def test_wallet_per_tx_stricter_shrinks(clock):
    v = check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=15.0), quote=quote(notional=15.0))
    c = names(v)
    assert v.approved and v.adjusted_notional == 15.0
    assert "reduced from 25.00 to 15.00" in c["wallet_per_tx_cap"].detail
    assert c["live_quote_notional"].passed and "set by wallet" in c["live_quote_notional"].detail


def test_risk_per_order_stricter_wins(clock):
    v = check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=500.0))
    assert v.approved and v.adjusted_notional == 25.0
    assert "fits" in names(v)["wallet_per_tx_cap"].detail


def test_quote_above_wallet_per_tx_blocked(clock):
    # approved size would be 15, a 25 USDT quote exceeds the wallet per-tx limit
    v = check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=15.0), quote=quote(notional=25.0))
    assert not v.approved and not names(v)["live_quote_notional"].passed


def test_wallet_daily_remainder_shrinks(clock):
    v = check(live_rm(clock, reserve_wallet_quota_for_exits=False),
              wallet_limits=wl(daily_limit_usdt=100.0, daily_used_usdt=88.0), quote=quote(notional=12.0))
    assert v.approved and v.adjusted_notional == 12.0
    assert "reduced from 25.00 to 12.00" in names(v)["wallet_daily_limit"].detail


def test_wallet_daily_quota_left_used_when_smaller(clock):
    w = wl(daily_limit_usdt=1000.0, daily_used_usdt=0.0, daily_left_usdt=11.0)
    assert w.daily_remaining_usdt() == 11.0
    v = check(live_rm(clock, reserve_wallet_quota_for_exits=False), wallet_limits=w, quote=quote(notional=11.0))
    assert v.approved and v.adjusted_notional == 11.0


def test_wallet_daily_exhausted_floors_at_zero_and_blocks(clock):
    w = wl(daily_limit_usdt=50.0, daily_used_usdt=70.0)
    assert w.daily_remaining_usdt() == 0.0
    v = check(live_rm(clock), wallet_limits=w)
    assert not v.approved and not names(v)["wallet_daily_limit"].passed and names(v)["min_order_notional"].passed
    assert "reduced from 25.00 to 0.00" in names(v)["wallet_daily_limit"].detail


def test_wallet_remainder_under_min_order_blocked(clock):
    v = check(live_rm(clock), wallet_limits=wl(daily_limit_usdt=100.0, daily_used_usdt=95.0))
    assert not v.approved and "too small" in names(v)["min_order_notional"].detail


# ------------------------------------------------------------------ allowed tokens
def test_disallowed_token_blocked(clock):
    v = check(live_rm(clock), wallet_limits=wl(allowed_tokens=["TSLAon", "AAPLon"]))
    c = names(v)["wallet_allowed_token"]
    assert not v.approved and not c.passed and "not on the wallet's allowed tokens" in c.detail


def test_allowed_token_case_insensitive(clock):
    v = check(live_rm(clock), wallet_limits=wl(allowed_tokens=["nvdaON"]))
    assert v.approved and names(v)["wallet_allowed_token"].passed


def test_allowed_token_by_address():
    w = wl(allowed_tokens=["0xABCdef"])
    assert w.allows("NVDAon", "0xabcDEF") and not w.allows("NVDAon", None)


def test_trade_all_tokens_false_without_list_passes_with_note(clock):
    v = check(live_rm(clock), wallet_limits=wl(trade_all_tokens=False))
    c = names(v)["wallet_allowed_token"]
    assert v.approved and c.passed and "not verified" in c.detail


# ------------------------------------------------------------------ missing limits / require flag
def test_no_wallet_limits_informational(clock):
    v = check(live_rm(clock))
    c = names(v)["wallet_limits"]
    assert v.approved and c.passed and "only our own LIVE limits apply" in c.detail
    assert "wallet_per_tx_cap" not in names(v)


def test_require_wallet_limits_blocks(clock):
    v = check(live_rm(clock, require_wallet_limits=True))
    assert not v.approved and not names(v)["wallet_limits"].passed
    v = check(live_rm(clock, require_wallet_limits=True), wallet_limits=wl(daily_limit_usdt=500.0))
    assert v.approved


# ------------------------------------------------------------------ SELLs never affected
def sell(symbol="NVDAon", reason=ExitReason.STOP_LOSS):
    return Signal(symbol=symbol, action=Action.SELL, source=AgentMode.AUTO, strategy="TREND_FOLLOWING", reason="x",
                  price=95.0, exit_reason=reason)


def test_protective_and_strategy_sells_ignore_wallet_and_token_guards(clock):
    rm = live_rm(clock, require_wallet_limits=True)
    for symbol in ("NVDAon", "TSLAx"):
        pf = lpf(cash=100.0, positions=[pos(symbol=symbol, qty=0.2, entry=100.0)])
        c = LiveContext(wallet_limits=wl(allowed_tokens=["OTHER"], per_tx_limit_usdt=1.0, daily_limit_usdt=0.0),
                        token_status=TokenStatus(trading_status="closed", reference_price_only=True, min_order_usdt=50))
        for s in (sell(symbol), sell(symbol, reason=None)):
            v = rm.check(s, pf, lctl(), context=c)
            assert v.approved and v.adjusted_notional > 0, v.reasons


def test_check_exits_unaffected(clock):
    rm = live_rm(clock, require_wallet_limits=True)
    pf = lpf(positions=[pos(symbol="TSLAx", qty=1.0, entry=100.0)])
    out = rm.check_exits(pf, {"TSLAx": 50.0})
    assert out and out[0].action == Action.SELL


# ------------------------------------------------------------------ live_limits
def test_live_limits_effective_and_source(clock):
    rm = live_rm(clock)
    d = rm.live_limits(lpf(), LiveContext(wallet_limits=wl(per_tx_limit_usdt=10.0, daily_limit_usdt=500.0,
                                                               daily_used_usdt=0.0, allowed_tokens=["NVDAon"])))
    assert d["per_order_cap_usdt"] == 10.0 and d["limit_source"]["per_order_cap_usdt"] == "wallet"
    assert d["daily_limit_usdt"] == 100.0 == d["daily_notional_cap_usdt"] and d["limit_source"]["daily_limit_usdt"] == "risk"
    assert d["allowed_tokens"] == ["NVDAon"] and d["wallet_limits"]["per_tx_limit_usdt"] == 10.0
    assert d["risk_limits"]["per_order_cap_usdt"] == 25.0

    d = rm.live_limits(None, LiveContext(wallet_limits=wl(daily_limit_usdt=60.0, daily_used_usdt=55.0)))
    assert d["per_order_cap_usdt"] == 25.0 and d["limit_source"]["per_order_cap_usdt"] == "risk"
    assert d["daily_limit_usdt"] == 60.0 and d["daily_used_usdt"] == 55.0 and d["daily_remaining_usdt"] == 5.0
    assert d["limit_source"]["daily_limit_usdt"] == "wallet"


def test_live_limits_without_wallet(clock):
    d = live_rm(clock).live_limits()
    assert d["wallet_limits"] is None and d["allowed_tokens"] is None
    assert d["per_order_cap_usdt"] == 25.0 and d["daily_limit_usdt"] == 100.0
    assert d["limit_source"] == {"per_order_cap_usdt": "risk", "daily_limit_usdt": "risk"}


# ------------------------------------------------------------------ from_baw
BAW_SETTINGS = {"success": True, "data": {
    "maxSigninDuration": "48h", "dailyLimit": 50000, "abnormalTxnHandling": "AutoReject", "tradeAllTokens": False,
    "predictionDailyLimit": 50000, "defiDailyLimit": 5000, "x402DailyLimit": 20,
    "quotaUsed": 120.5, "quotaLeft": 49879.5, "quotaDate": "2026-04-03"}}


def test_from_baw_documented_fields():
    w = WalletLimits.from_baw(BAW_SETTINGS, fetched_at=T0)
    assert (w.daily_limit_usdt, w.daily_used_usdt, w.daily_left_usdt) == (50000.0, 120.5, 49879.5)
    assert w.quota_date == "2026-04-03" and w.trade_all_tokens is False and w.source == "baw" and w.fetched_at == T0
    assert w.per_tx_limit_usdt is None and w.allowed_tokens is None  # not documented by baw
    assert WalletLimits.from_baw(BAW_SETTINGS["data"]).daily_limit_usdt == 50000.0  # bare data also accepted


def test_from_baw_tolerant_provisional_keys():
    w = WalletLimits.from_baw({"perTxLimit": "30", "dailyLimit": "200", "quotaUsed": "bad", "tradeAllTokens": "false",
                               "allowedTokens": [{"symbol": "NVDAon", "contractAddress": "0xAbC"}, "TSLAon"],
                               "allowedContracts": "0x1, 0x2", "somethingElse": 1})
    assert w.per_tx_limit_usdt == 30.0 and w.daily_limit_usdt == 200.0 and w.daily_used_usdt is None
    assert w.allowed_tokens == ["NVDAon", "0xAbC", "TSLAon"] and w.allowed_contracts == ["0x1", "0x2"]
    assert w.allows("0xabc") and not w.allows("AAPLon")


def test_from_baw_trade_all_tokens_true_and_explicit_list():
    assert WalletLimits.from_baw({"tradeAllTokens": True, "allowedTokens": ["A"]}).allowed_tokens is None
    assert WalletLimits.from_baw(BAW_SETTINGS, allowed_tokens=["NVDAon"]).allowed_tokens == ["NVDAon"]
    assert WalletLimits.from_baw({}).daily_remaining_usdt() is None


# ------------------------------------------------------------------ token status
def test_token_status_missing_is_informational(clock):
    v = check(live_rm(clock))
    assert v.approved and names(v)["live_token_status"].passed


def test_token_status_trading_passes(clock):
    v = check(live_rm(clock), token_status=TokenStatus(trading_status="trading"))
    assert v.approved and names(v)["live_token_status"].detail == "NVDAon is trading"


def test_token_status_offhours_blocks(clock):
    v = check(live_rm(clock), token_status={"trading_status": "offhours", "reference_price_only": True})
    c = names(v)["live_token_status"]
    assert not v.approved and not c.passed and "NVDAon is off-hours - price is reference only" in c.detail


def test_token_status_closed_and_no_liquidity_block(clock):
    for st in ("closed", "no_liquidity", "ASSET_PAUSED"):
        assert not check(live_rm(clock), token_status=TokenStatus(trading_status=st)).approved


def test_reference_price_only_blocks_even_when_trading(clock):
    v = check(live_rm(clock), token_status=TokenStatus(trading_status="trading", reference_price_only=True))
    assert not v.approved and "reference only" in names(v)["live_token_status"].detail


# ------------------------------------------------------------------ token minimum order
def test_token_min_order_blocks_below(clock):
    v = check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=15.0), quote=quote(notional=15.0),
              token_status=TokenStatus(min_order_usdt=20.0))
    c = names(v)["min_order_notional"]
    assert not v.approved and "below token minimum order" in c.detail
    assert not names(v)["wallet_per_tx_cap"].passed and "only 15.00 USDT left" in names(v)["wallet_per_tx_cap"].detail
    # with no cap cutting it, the token minimum itself is the blocking rule
    v = check(live_rm(clock, bump_to_min_order=False), sig=lbuy(proposed_notional=12.0), quote=quote(notional=12.0),
              token_status=TokenStatus(min_order_usdt=30.0))
    assert not v.approved and not names(v)["min_order_notional"].passed


def test_token_min_order_passes_above(clock):
    v = check(live_rm(clock), token_status=TokenStatus(min_order_usdt=20.0))
    assert v.approved and v.adjusted_notional == 25.0 and "token minimum" in names(v)["min_order_notional"].detail


def test_token_min_below_global_min_keeps_10(clock):
    v = check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=8.0), quote=quote(notional=8.0),
              token_status=TokenStatus(min_order_usdt=5.0))
    c = names(v)["min_order_notional"]
    assert not v.approved and "minimum 10.00" in c.detail and "too small" in c.detail


# ------------------------------------------------------------------ blocked symbol suffixes
def test_xstock_blocked_by_default(clock):
    v = check(live_rm(clock), sig=lbuy("TSLAx"), quote=quote(symbol="TSLAx"))
    c = names(v)["live_symbol_allowed"]
    assert not v.approved and not c.passed and "TSLAx" in c.detail


def test_suffix_case_sensitive(clock):
    v = check(live_rm(clock), sig=lbuy("NVDAX"), quote=quote(symbol="NVDAX"))
    assert v.approved and names(v)["live_symbol_allowed"].passed
    assert check(live_rm(clock)).approved  # NVDAon


def test_suffixes_configurable(clock):
    assert check(live_rm(clock, blocked_symbol_suffixes=[]), sig=lbuy("TSLAx"), quote=quote(symbol="TSLAx")).approved
    assert not check(live_rm(clock, blocked_symbol_suffixes=["on"])).approved


# ------------------------------------------------------------------ exit-quota reserve (wallet dailyLimit counts SELLs)
def multi_rm(clock, **profile):
    rm = live_rm(clock, **profile)
    cfg = rm.config
    rm.set_config(cfg.model_copy(update={"position_rules": cfg.position_rules.model_copy(update={"max_open_positions": 5})}))
    return rm


def test_exit_reserve_shrinks_buy(clock):
    # quota left 60, open exposure 20 -> N <= (60 - 20) / 2 = 20
    pf = lpf(cash=200.0, positions=[pos(symbol="AAPLon", qty=0.2, entry=100.0)])
    v = check(multi_rm(clock), pf=pf, wallet_limits=wl(daily_limit_usdt=100.0, daily_used_usdt=40.0), quote=quote(notional=20.0))
    c = names(v)["wallet_exit_reserve"]
    assert v.approved and v.adjusted_notional == 20.0
    assert "reduced from 25.00 to 20.00" in c.detail and "keeping 20.00 USDT of it to sell open positions" in c.detail


def test_exit_reserve_then_min_order_blocks(clock):
    # quota left 30, exposure 20 -> N <= 5 -> below 10 minimum
    pf = lpf(cash=200.0, positions=[pos(symbol="AAPLon", qty=0.2, entry=100.0)])
    v = check(multi_rm(clock), pf=pf, wallet_limits=wl(daily_left_usdt=30.0))
    assert not v.approved and "reduced from" in names(v)["wallet_exit_reserve"].detail
    assert not names(v)["wallet_exit_reserve"].passed and names(v)["min_order_notional"].passed


def test_exit_reserve_floors_at_zero_when_exposure_exceeds_quota(clock):
    pf = lpf(cash=200.0, positions=[pos(symbol="AAPLon", qty=0.4, entry=100.0)])
    v = check(multi_rm(clock), pf=pf, wallet_limits=wl(daily_left_usdt=30.0))
    assert not v.approved and "to 0.00 USDT" in names(v)["wallet_exit_reserve"].detail


def test_exit_reserve_fits_and_can_be_disabled(clock):
    v = check(live_rm(clock), wallet_limits=wl(daily_limit_usdt=1000.0))
    assert v.approved and "fits" in names(v)["wallet_exit_reserve"].detail
    v = check(live_rm(clock, reserve_wallet_quota_for_exits=False), wallet_limits=wl(daily_limit_usdt=1000.0))
    assert "wallet_exit_reserve" not in names(v)
    assert "wallet_exit_reserve" not in names(check(live_rm(clock), wallet_limits=wl(per_tx_limit_usdt=100.0)))


def test_exit_reserve_never_touches_sells(clock):
    pf = lpf(cash=100.0, positions=[pos(symbol="NVDAon", qty=0.5, entry=100.0)])
    c = LiveContext(wallet_limits=wl(daily_left_usdt=1.0))
    for s in (sell(), sell(reason=None)):
        v = live_rm(clock).check(s, pf, lctl(), context=c)
        assert v.approved and "wallet_exit_reserve" not in names(v)


def test_exit_quota_shortfall_in_live_limits(clock):
    rm = live_rm(clock)
    pf = lpf(positions=[pos(symbol="AAPLon", qty=0.5, entry=100.0)])  # exposure 50
    assert rm.live_limits(pf, LiveContext(wallet_limits=wl(daily_left_usdt=30.0)))["exit_quota_shortfall_usdt"] == 20.0
    assert rm.live_limits(pf, LiveContext(wallet_limits=wl(daily_left_usdt=80.0)))["exit_quota_shortfall_usdt"] == 0.0
    assert rm.live_limits(pf)["exit_quota_shortfall_usdt"] is None
    assert rm.live_limits(None, LiveContext(wallet_limits=wl(daily_left_usdt=1.0)))["exit_quota_shortfall_usdt"] is None


# ------------------------------------------------------------------ exchange price impact
def test_price_impact_from_context_overrides_reference(clock):
    # reference comparison alone would be 5% (blocked); exchange says 0.3%
    v = check(live_rm(clock), price_impact_pct=0.3, quote=quote(price=105.0, notional=25.0))
    c = names(v)["live_slippage"]
    assert v.approved and c.passed and "Exchange-reported price impact 0.30%" in c.detail


def test_price_impact_from_context_blocks(clock):
    v = check(live_rm(clock), price_impact_pct=-2.5)
    c = names(v)["live_slippage"]
    assert not v.approved and "Exchange-reported price impact 2.50%" in c.detail and "too much slippage" in c.detail


def test_price_impact_works_without_reference(clock):
    v = check(live_rm(clock), price_impact_pct=0.2, reference_price=None, sig=lbuy(price=None))
    assert v.approved and "Exchange-reported" in names(v)["live_slippage"].detail


def test_price_impact_absent_uses_reference_fallback(clock):
    v = check(live_rm(clock))
    assert v.approved and "vs reference" in names(v)["live_slippage"].detail


# ------------------------------------------------------------------ live_limits warnings
def test_live_limits_warnings_empty_by_default(clock):
    assert live_rm(clock).live_limits()["warnings"] == []
    assert live_rm(clock).live_limits(lpf(), LiveContext(wallet_limits=wl(daily_left_usdt=80.0), now=T0))["warnings"] == []


def test_live_limits_warning_exit_quota_shortfall(clock):
    pf = lpf(positions=[pos(symbol="AAPLon", qty=0.5, entry=100.0)])  # exposure 50
    w = live_rm(clock).live_limits(pf, LiveContext(wallet_limits=wl(daily_left_usdt=30.0)))["warnings"]
    assert w == [{"code": "exit_quota_shortfall", "message": (
        "Wallet daily quota left (30.00 USDT) is below open positions (50.00 USDT) - not every position can be sold today")}]


def test_live_limits_warning_wallet_limits_stale(clock):
    from datetime import timedelta
    rm = live_rm(clock)
    old = LiveContext(now=T0, wallet_limits=wl(daily_limit_usdt=500.0, fetched_at=T0 - timedelta(minutes=20)))
    fresh = LiveContext(now=T0, wallet_limits=wl(daily_limit_usdt=500.0, fetched_at=T0 - timedelta(minutes=5)))
    w = rm.live_limits(None, old)["warnings"]
    assert [x["code"] for x in w] == ["wallet_limits_stale"] and "20 min ago" in w[0]["message"]
    assert rm.live_limits(None, fresh)["warnings"] == []


def test_from_baw_reads_fetched_at_from_payload():
    from datetime import datetime, timezone
    w = WalletLimits.from_baw({"success": True, "data": {"dailyLimit": "100"}, "fetched_at": "2026-10-11T02:00:00Z"})
    assert w.fetched_at == datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc) and w.daily_limit_usdt == 100
    w2 = WalletLimits.from_baw({"dailyLimit": 50, "fetched_at": datetime(2026, 10, 11, tzinfo=timezone.utc)})
    assert w2.fetched_at.day == 11

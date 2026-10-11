"""Watchlist (up to 5 tokens traded together): caps count every symbol together, and two BUYs approved in the same
tick can't jointly exceed a cap when the caller reserves them (``check(..., reserve_as=key)``)."""
import pytest

from backend.contracts import OrderStatus, TradingMode
from backend.risk import DefaultRiskManager, RiskConfig

from .conftest import buy, ctl, make_portfolio, pos
from .test_live import ctx, fill, lbuy, lctl, live_rm, names

WATCHLIST = ["NVDAon", "AAPLon", "TSLAon", "MSFTon", "AMZNon"]


def lpf(cash=1000.0, positions=()):
    return make_portfolio(cash=cash, positions=positions, starting_capital=1000.0, mode=TradingMode.LIVE)


def lctx():
    return ctx(quote=None, balances={"USDT": 1000.0, "BNB": 0.01})


def test_without_reservation_same_tick_buys_would_exceed_live_exposure(clock):
    """The problem: the portfolio isn't updated between checks, so every symbol sees the full 50 USDT room."""
    rm = live_rm(clock)
    pf = lpf()
    vs = [rm.check(lbuy(s), pf, lctl(), context=lctx()) for s in WATCHLIST]
    assert all(v.approved for v in vs) and sum(v.adjusted_notional for v in vs) == pytest.approx(125.0)


def test_reserved_same_tick_buys_respect_live_total_exposure(clock):
    rm = live_rm(clock)
    pf = lpf()
    vs = [rm.check(lbuy(s), pf, lctl(), context=lctx(), reserve_as=f"d-{s}") for s in WATCHLIST]
    approved = [v for v in vs if v.approved]
    assert [v.adjusted_notional for v in approved] == [pytest.approx(25.0)] * 2
    assert sum(v.adjusted_notional for v in approved) <= 50.0 + 1e-9
    third = vs[2]
    assert not third.approved and "LIVE exposure limit reached" in " ".join(third.reasons)
    assert "2 other approved BUY(s)" in names(third)["reserved_orders"].detail
    assert {r["key"] for r in rm.reservations()} == {"d-NVDAon", "d-AAPLon"}


def test_reserved_same_tick_buys_respect_live_daily_buy_cap(clock):
    rm = live_rm(clock)
    rm.set_live_limits(dict(max_order_usdt=40, max_total_usdt=500, daily_buy_usdt=100, order_usdt=40))
    pf = lpf()
    vs = [rm.check(lbuy(s), pf, lctl(), context=lctx(), reserve_as=s) for s in WATCHLIST[:3]]
    assert [round(v.adjusted_notional, 6) for v in vs] == [40.0, 40.0, 20.0]  # 100 daily cap shared


def test_per_token_cap_is_per_symbol_and_counts_reservations(clock):
    rm = DefaultRiskManager(ctl(), config=RiskConfig(), clock=clock)  # SIM: 30% per token, 60% total, 15% size
    pf = make_portfolio(cash=10_000)
    a1 = rm.check(buy("NVDAon"), pf, ctl(), reserve_as="a1")
    a2 = rm.check(buy("NVDAon"), pf, ctl(), reserve_as="a2")
    a3 = rm.check(buy("NVDAon"), pf, ctl(), reserve_as="a3")
    assert (a1.adjusted_notional, a2.adjusted_notional) == (1500.0, 1500.0)
    assert not a3.approved and "NVDAon exposure limit reached" in " ".join(a3.reasons)
    rm.clear_reservations()
    rm.reserve_buy("x", "NVDAon", 3000)  # NVDAon full; another symbol still has its own room
    b = rm.check(buy("AAPLon"), pf, ctl())
    assert b.approved and b.adjusted_notional == pytest.approx(1500.0)


def test_total_exposure_counts_reservations_across_symbols(clock):
    rm = DefaultRiskManager(ctl(), config=RiskConfig(default_max_open_positions=10), clock=clock)
    pf = make_portfolio(cash=10_000)
    vs = [rm.check(buy(s), pf, ctl(), reserve_as=s) for s in WATCHLIST]
    assert [round(v.adjusted_notional) for v in vs] == [1500, 1500, 1500, 1500, 0]  # 60% of 10,000 = 6,000 total
    assert "Total exposure limit reached" in " ".join(vs[4].reasons)


def test_max_open_positions_across_symbols(clock):
    rm = DefaultRiskManager(ctl(), config=RiskConfig(), clock=clock)  # default 3
    held = [pos(symbol="NVDAon", qty=1, entry=100)]
    pf = make_portfolio(cash=9_900, positions=held)
    vs = [rm.check(buy(s), pf, ctl(), reserve_as=s) for s in WATCHLIST[1:]]
    assert [v.approved for v in vs] == [True, True, False, False]
    assert "3 open position(s) incl. 2 approved BUY(s) not executed yet, limit 3 - limit reached" in names(vs[2])["max_open_positions"].detail
    pf3 = make_portfolio(cash=9_700, positions=held + [pos(symbol="AAPLon", qty=1, entry=100), pos(symbol="TSLAon", qty=1, entry=100)])
    rm.clear_reservations()
    assert not rm.check(buy("MSFTon"), pf3, ctl()).approved  # held positions of all symbols count


def test_recheck_same_key_not_double_counted_and_blocked_recheck_drops_it(clock):
    rm = live_rm(clock)
    pf = lpf()
    assert rm.check(lbuy("NVDAon"), pf, lctl(), context=lctx(), reserve_as="d1").adjusted_notional == 25
    v = rm.check(lbuy("NVDAon"), pf, lctl(), context=lctx(), reserve_as="d1")  # pass 2 / approval re-check
    assert v.approved and v.adjusted_notional == 25 and "reserved_orders" not in names(v)
    rm.pause("x")
    assert not rm.check(lbuy("NVDAon"), pf, lctl(), context=lctx(), reserve_as="d1").approved
    assert rm.reservations() == []


def test_release_paths(clock):
    rm = live_rm(clock)
    pf = lpf()
    rm.check(lbuy("NVDAon"), pf, lctl(), context=lctx(), reserve_as="d1")
    rm.check(lbuy("AAPLon"), pf, lctl(), context=lctx(), reserve_as="d2")
    assert rm.release_reservation("d1") and not rm.release_reservation("d1")
    rm.record_execution(fill(25, status=OrderStatus.FAILED, coid="d2"))  # result with client_order_id == key
    assert rm.reservations() == []
    rm.reserve_buy("d3", "TSLAon", 25, ttl_seconds=60)
    clock.advance(seconds=61)
    assert rm.reservations() == []  # expired


def test_after_release_the_portfolio_carries_the_order(clock):
    """Loop pattern: reserve, execute, release, rebuild the portfolio -> exposure is counted exactly once."""
    rm = live_rm(clock)
    v1 = rm.check(lbuy("NVDAon"), lpf(), lctl(), context=lctx(), reserve_as="d1")
    rm.release_reservation("d1")
    pf = lpf(cash=975, positions=[pos(symbol="NVDAon", qty=0.25, entry=100)])
    v2 = rm.check(lbuy("AAPLon"), pf, lctl(), context=lctx(), reserve_as="d2")
    assert v1.adjusted_notional == 25 and v2.approved and v2.adjusted_notional == pytest.approx(25.0)
    assert not rm.check(lbuy("TSLAon"), pf, lctl(), context=lctx(), reserve_as="d3").approved


def test_bad_reservation_args(clock):
    rm = live_rm(clock)
    with pytest.raises(ValueError):
        rm.reserve_buy("", "X", 10)
    with pytest.raises(ValueError):
        rm.reserve_buy("k", "X", 0)

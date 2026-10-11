"""LIVE live_slippage: adverse vs favorable price impact (bot3/polish)."""
import pytest

from backend.contracts import OrderSide
from backend.risk import LiveRiskProfile, RiskConfig
from backend.risk.manager import quote_impact_pct

from .test_live import ctx, lbuy, lctl, live_rm, lpf, names, quote


def slip(clock, qprice, ref=100.0, bps=50, **profile):
    v = live_rm(clock, **profile).check(lbuy(), lpf(), lctl(),
                                        context=ctx(reference_price=ref, quote=quote(price=qprice, slippage_bps=bps)))
    return v, names(v)["live_slippage"]


def test_default_and_env(monkeypatch):
    assert LiveRiskProfile().max_favorable_impact_pct == 3.0
    monkeypatch.setenv("RISK_LIVE_MAX_FAVORABLE_IMPACT_PCT", "5")
    assert RiskConfig.from_env().live.max_favorable_impact_pct == 5.0


def test_adverse_buy_over_cap_blocked(clock):
    v, c = slip(clock, 101.5)
    assert not v.approved and not c.passed
    assert "1.50% worse" in c.detail and "adverse" in c.detail and "too much slippage" in c.detail


def test_adverse_buy_under_cap_allowed(clock):
    v, c = slip(clock, 100.8)
    assert v.approved and c.passed and "0.80% worse" in c.detail and "adverse" in c.detail


@pytest.mark.parametrize("qprice", [99.0, 98.0, 97.1])
def test_small_favorable_buy_allowed(clock, qprice):
    # previously abs() blocked anything over 1% even when the quote was cheaper than the reference
    v, c = slip(clock, qprice)
    assert v.approved and c.passed and "better than the reference" in c.detail and "favorable" in c.detail


def test_huge_favorable_buy_blocked_as_bad_quote(clock):
    v, c = slip(clock, 95.0)
    assert not v.approved and not c.passed
    assert "Quote is 5.00% better than the reference price" in c.detail
    assert "above the 3.00% sanity limit; likely a bad quote" in c.detail


def test_favorable_cap_configurable(clock):
    v, _ = slip(clock, 95.0, max_favorable_impact_pct=6.0)
    assert v.approved


def test_favorable_still_checks_slippage_tolerance(clock):
    v, c = slip(clock, 99.0, bps=150)
    assert not v.approved and "slippage tolerance 1.50%" in c.detail and "tolerance too high" in c.detail


def test_exactly_at_limits(clock):
    assert slip(clock, 101.0)[0].approved      # 1.00% adverse == cap
    assert slip(clock, 97.0)[0].approved       # 3.00% favorable == sanity cap


@pytest.mark.parametrize("side,qprice,expected", [
    (OrderSide.BUY, 102.0, (2.0, 0.0)),    # paying more: adverse
    (OrderSide.BUY, 98.0, (0.0, 2.0)),     # paying less: favorable
    (OrderSide.SELL, 98.0, (2.0, 0.0)),    # receiving less: adverse (mirrored)
    (OrderSide.SELL, 102.0, (0.0, 2.0)),   # receiving more: favorable (mirrored)
    (OrderSide.SELL, 100.0, (0.0, 0.0)),
])
def test_quote_impact_direction(side, qprice, expected):
    assert quote_impact_pct(side, qprice, 100.0) == pytest.approx(expected)


def test_quote_impact_needs_reference():
    with pytest.raises(ValueError):
        quote_impact_pct(OrderSide.BUY, 100.0, 0.0)


@pytest.mark.parametrize("impact,ok", [(0.5, True), (-0.5, True), (1.5, False), (-1.5, False)])
def test_exchange_reported_impact_stays_sign_blind(clock, impact, ok):
    # Web3 priceImpactPercent sign convention is undocumented -> magnitude counts as adverse either way
    v = live_rm(clock).check(lbuy(), lpf(), lctl(), context=ctx(price_impact_pct=impact))
    c = names(v)["live_slippage"]
    assert c.passed is ok and "Exchange-reported price impact" in c.detail and "counted as adverse" in c.detail

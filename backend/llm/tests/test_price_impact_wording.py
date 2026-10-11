"""Price-impact wording (risk 15beac5 directional live_slippage, engine c89f7e0 unsigned live_price_impact). Offline.

Detail strings mirror Bot 3's / Bot 2's f-strings exactly (backend/llm must not import risk or engine code)."""
import json
import re

import pytest

from backend.contracts import (
    Action, AgentMode, ControlState, Decision, OrderSide, Portfolio, Regime, RiskCheck, RiskVerdict, Signal,
    StatusSnapshot, TradingMode,
)
from backend.llm import DefaultExplainer, live, templates

MODES = [TradingMode.SIM, TradingMode.PAPER, TradingMode.LIVE]
SIDES = [OrderSide.BUY, OrderSide.SELL]
TOL = "slippage tolerance 0.50% (max 1.00%)"
UNSIGNED = "Binance didn't say which way the price impact goes, so Regime treated it as a cost to be safe"
RAW = re.compile(r"live_slippage|live_price_impact|max_favorable_impact_pct|RISK_LIVE|direction not reported|"
                 r"counted as adverse|sanity limit|reference price", re.I)


# ------------------------------------------------------------------ risk / engine detail builders (exact formats)
def adverse(pct, q, ref=100.0, cap=1.0):
    return (f"Quote is {pct:.2f}% worse than the reference price (adverse; quote {q:.4f} vs reference {ref:.4f}), "
            f"max {cap:.2f}% - too much slippage; {TOL}")


def favorable_bad(pct, q, ref=100.0, cap=3.0):
    return (f"Quote is {pct:.2f}% better than the reference price (quote {q:.4f} vs reference {ref:.4f}), above the "
            f"{cap:.2f}% sanity limit; likely a bad quote; {TOL}")


def favorable_ok(pct, q, ref=100.0, cap=3.0):
    return (f"Quote is {pct:.2f}% better than the reference price (favorable; quote {q:.4f} vs reference {ref:.4f}), "
            f"allowed up to {cap:.2f}%; {TOL}")


def unsigned_risk(pct, cap=1.0):
    return (f"Exchange-reported price impact {abs(pct):.2f}% (direction not reported, counted as adverse; "
            f"max {cap:.2f}%), {TOL} - too much slippage")


def unsigned_engine(pct, cap=1.0):
    return f"quote price impact {abs(pct):.4g}% > {cap:g}% (direction not reported, counted as adverse)"


# ------------------------------------------------------------------ decisions
def _sig(side=OrderSide.BUY, **kw):
    action = Action.BUY if side == OrderSide.BUY else Action.SELL
    base = dict(symbol="NVDAB", action=action, source=AgentMode.AUTO, strategy="GRID", regime=Regime.SIDEWAYS,
                reason="price in the bottom grid cell", price=100.0)
    base.update(kw)
    return Signal(**base)


def risk_decision(mode, side, detail, passed=False):
    sig = _sig(side)
    check = RiskCheck(name="live_slippage", passed=passed, detail=detail)
    if passed:
        v = RiskVerdict(signal_id=sig.id, approved=True, adjusted_size_pct=10, adjusted_notional=10.0, checks=[check])
        action = sig.action
    else:
        v = RiskVerdict(signal_id=sig.id, approved=False, reasons=[f"Blocked (live_slippage): {detail}"], checks=[check])
        action = Action.STAY_OUT
    return Decision(mode=mode, agent_mode=AgentMode.AUTO, symbol="NVDAB", regime=Regime.SIDEWAYS, strategy="GRID",
                    action=action, reason=sig.reason if passed else f"{sig.action.value} not executed: Blocked (live_slippage): {detail}",
                    signal=sig, risk_verdict=v)


def engine_decision(mode, detail, verdict_detail=None):
    sig = Signal(symbol="NVDAB", action=Action.STAY_OUT, source=AgentMode.AUTO, strategy="GRID", regime=Regime.SIDEWAYS,
                 conditions_failed=["live_price_impact"], reason=f"Live safety blocked entry: {detail}")
    v = None
    if verdict_detail is not None:
        v = RiskVerdict(signal_id=sig.id, approved=False, reasons=[f"Blocked (live_slippage): {verdict_detail}"])
    return Decision(mode=mode, agent_mode=AgentMode.AUTO, symbol="NVDAB", regime=Regime.SIDEWAYS, strategy="GRID",
                    action=Action.STAY_OUT, reason=sig.reason, signal=sig, risk_verdict=v)


def _clean_text(txt):
    assert not RAW.search(txt), txt
    assert not live.has_env_names(txt), txt


# ------------------------------------------------------------------ the four cases, buy + sell, every mode
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
def test_favorable_within_cap_has_no_block_wording(mode, side):
    q = 98.0 if side == OrderSide.BUY else 102.0  # cheaper buy / richer sell = favorable
    txt = templates.explain_decision(risk_decision(mode, side, favorable_ok(2.0, q), passed=True))
    for bad in ("bad quote", "worse than", "slippage", "price impact", "blocked", "suspicious"):
        assert bad not in txt.lower(), txt
    _clean_text(txt)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
def test_favorable_beyond_cap_is_a_likely_bad_quote(mode, side):
    q = 95.0 if side == OrderSide.BUY else 105.0
    txt = templates.explain_decision(risk_decision(mode, side, favorable_bad(5.0, q)))
    assert ("the quote was suspiciously better than the market price (5% better, more than 3%), so Regime skipped it "
            "as a likely bad quote") in txt
    assert "worse than" not in txt and "slippage" not in txt
    _clean_text(txt)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
def test_adverse_keeps_slippage_wording(mode, side):
    q = 101.5 if side == OrderSide.BUY else 98.5  # dearer buy / cheaper sell = adverse
    txt = templates.explain_decision(risk_decision(mode, side, adverse(1.5, q)))
    assert "the quote's price was 1.5% worse than the market price, more than the 1% slippage limit" in txt
    assert "bad quote" not in txt and UNSIGNED not in txt
    _clean_text(txt)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("impact", [1.5, -1.5])
def test_unsigned_impact_counted_as_cost(mode, side, impact):
    txt = templates.explain_decision(risk_decision(mode, side, unsigned_risk(impact)))
    assert f"{UNSIGNED}, and at 1.5% it is over the 1% limit" in txt
    assert "favorable" not in txt.lower() and "better" not in txt
    _clean_text(txt)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("impact", [1.5, -1.5, 3])
def test_engine_stay_out_matches_risk_wording(mode, impact):
    eng = templates.explain_decision(engine_decision(mode, unsigned_engine(impact)))
    risk = live.plain_reason(f"Blocked (live_slippage): {unsigned_risk(impact)}")
    assert risk.startswith(UNSIGNED) and f"Stayed out because {risk}." in eng
    _clean_text(eng)


@pytest.mark.parametrize("mode", MODES)
def test_engine_and_risk_same_situation_said_once(mode):
    d = engine_decision(mode, unsigned_engine(1.5), verdict_detail=unsigned_risk(1.5))
    txt = templates.explain_decision(d)
    assert txt.count(UNSIGNED) == 1, txt
    assert "for the same reason" in txt
    _clean_text(txt)


def test_engine_code_without_detail_still_plain():
    assert live.plain_reason("live_price_impact").startswith(UNSIGNED)
    assert live.plain_reason("live_price_impact: " + unsigned_engine(2)) == f"{UNSIGNED}, and at 2% it is over the 1% limit"


# ------------------------------------------------------------------ edge cases, defensive parsing
def test_tolerance_only_failure_with_small_favorable_quote():
    det = ("Quote is 1.00% better than the reference price (favorable; quote 99.0000 vs reference 100.0000), allowed "
           "up to 3.00%; slippage tolerance 1.50% (max 1.00%) - slippage tolerance too high")
    txt = live.plain_reason(f"Blocked (live_slippage): {det}")
    assert txt == "the allowed slippage on the swap (1.5%) is above the 1% limit"


def test_unsigned_within_limit_but_tolerance_too_high():
    det = ("Exchange-reported price impact 0.40% (direction not reported, counted as adverse; max 1.00%), "
           "slippage tolerance 1.50% (max 1.00%) - too much slippage")
    assert live.plain_reason(f"Blocked (live_slippage): {det}") == "the allowed slippage on the swap (1.5%) is above the 1% limit"


@pytest.mark.parametrize("detail", [
    "Quote price 183.1 vs reference 180.0: impact 1.72%, slippage tolerance 0.50%; max 1.0% - too much slippage",
    "Exchange-reported price impact 1.5%, slippage tolerance 0.50%; max 1.0% - too much slippage",
    "", None, 42,
])
def test_old_or_odd_details_fall_back_to_generic(detail):
    assert live.price_impact_text("live_slippage", detail) is None
    txt = live.plain_reason(f"Blocked (live_slippage): {detail if detail is not None else ''}")
    assert "over the cap" in txt or "slippage" in txt


def test_missing_numbers_do_not_crash():
    assert live.price_impact_text("live_slippage", "likely a bad quote") == (
        "the quote was suspiciously better than the market price (far more than normal), so Regime skipped it as a "
        "likely bad quote")
    assert live.price_impact_text("live_slippage", "direction not reported").startswith(UNSIGNED)


def test_custom_caps_are_shown():
    assert "(7% better, more than 6%)" in live.plain_reason(
        f"Blocked (live_slippage): {favorable_bad(7.0, 93.0, cap=6.0)}")
    assert "more than the 0.5% slippage limit" in live.plain_reason(
        f"Blocked (live_slippage): {adverse(0.8, 100.8, cap=0.5)}")


def test_alert_free_text_is_humanized():
    assert templates.humanize_reason("Live safety blocked entry: " + unsigned_engine(1.5)) == (
        f"Live safety blocked entry: {UNSIGNED}, and at 1.5% it is over the 1% limit")


# ------------------------------------------------------------------ Grok prompt facts + rules
def test_grok_facts_dedupe_and_rules(make_client, reply):
    client, rec = make_client(reply("Regime stayed out of NVDAB."))
    DefaultExplainer(client).explain_decision(engine_decision(TradingMode.LIVE, unsigned_engine(1.5),
                                                               verdict_detail=unsigned_risk(1.5)))
    msgs = rec.payloads[0]["messages"]
    facts = json.loads(msgs[1]["content"].split("LIVE FACTS JSON:\n", 1)[1].split("\n\nDECISION JSON", 1)[0])
    assert facts["engine_reasons"] == [f"{UNSIGNED}, and at 1.5% it is over the 1% limit"]
    assert facts["block_reasons"] == [] and facts["reason"] == ""
    sysmsg = msgs[0]["content"]
    assert "Regime" in sysmsg and "likely bad quote" in sysmsg and "never call such an impact favorable" in sysmsg


def test_status_line_says_regime():
    base = dict(portfolio=Portfolio(cash=100, equity=100, starting_capital=100))
    on = templates.control_line(StatusSnapshot(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False), **base))
    assert on.startswith("Regime is trading autonomously 24/7 with real funds on BNB Chain mainnet")
    sim = templates.control_line(StatusSnapshot(control=ControlState(trading_mode=TradingMode.SIM), **base))
    assert sim.startswith("Regime is running in SIM mode")

"""LIVE mode (real funds, BNB Chain mainnet, autonomous 24/7): templates, reason mapping, Grok prompt + guard. Offline."""
import json
import re

import pytest

from backend.contracts import (
    Action, AgentMode, ControlState, Decision, ExecutionPath, ExecutionResult, ExitReason, OrderSide, OrderStatus,
    Portfolio, Regime, RiskCheck, RiskVerdict, Signal, StatusSnapshot, TradingMode,
)
from backend.llm import DefaultExplainer, live, templates

TX = "0x" + "ab12" * 16
APPROVAL = re.compile(r"confirm|approv|await|waiting for your", re.I)


def _sig(**kw):
    base = dict(symbol="NVDAB", action=Action.BUY, source=AgentMode.AUTO, strategy="TREND_FOLLOWING",
                regime=Regime.STRONG_UPTREND, reason="price above rising EMA20", conditions_met=["price_above_ema"], price=182.5)
    base.update(kw)
    return Signal(**base)


def _decision(sig=None, verdict=None, ex=None, action=Action.BUY, mode=TradingMode.LIVE):
    sig = sig or _sig()
    verdict = verdict if verdict is not None else RiskVerdict(signal_id=sig.id, approved=True, adjusted_size_pct=2.5,
                                                              adjusted_notional=25.0)
    return Decision(mode=mode, agent_mode=AgentMode.AUTO, symbol=sig.symbol, regime=Regime.STRONG_UPTREND,
                    strategy=sig.strategy, action=action, reason="trend entry", signal=sig, risk_verdict=verdict, execution=ex)


def _ex(**kw):
    base = dict(client_order_id="c1", symbol="NVDAB", side=OrderSide.BUY, status=OrderStatus.FILLED, path=ExecutionPath.SWAP,
                mode=TradingMode.LIVE, filled_quantity=0.137, avg_price=182.4, notional=24.99, fee=0.02, tx_hash=TX)
    base.update(kw)
    return ExecutionResult(**base)


def _blocked(*reasons, checks=()):
    sig = _sig()
    v = RiskVerdict(signal_id=sig.id, approved=False, reasons=list(reasons), checks=list(checks))
    return _decision(sig, v, None, action=Action.STAY_OUT)


# ------------------------------------------------------------------ executions
def test_live_filled_buy_names_real_funds_amount_route_fill_tx_and_bscscan():
    txt = templates.explain_decision(_decision(ex=_ex()))
    assert "LIVE, real funds, autonomous" in txt
    assert "Regime bought NVDAB on its own with real funds on BNB Chain mainnet: 24.99 USDT via a swap at a fill price of 182.4" in txt
    assert f"Tx hash {TX}: https://bscscan.com/tx/{TX}." in txt
    assert not APPROVAL.search(txt)


def test_autonomous_live_never_uses_confirmation_wording():
    # AUTO_EXECUTE (confirmation off): no approval wording. Confirmation mode (opt-in) is in test_explain_fixes.py.
    sig = _sig()
    v = RiskVerdict(signal_id=sig.id, approved=True, adjusted_size_pct=2.5, adjusted_notional=25.0, requires_confirmation=False)
    for d in (_decision(sig, v, None), _decision(sig, v, _ex())):
        txt = templates.explain_decision(d)
        assert not APPROVAL.search(txt), txt
    assert "placing this order on its own" in templates.explain_decision(_decision(sig, v, None))


def test_live_rfq_sell_exit_with_dict_like_extra_fields():
    sig = _sig(action=Action.SELL, exit_reason=ExitReason.TRAILING_STOP, position_id="pos0001aaaa", symbol="NVDAon")
    ex = _ex(symbol="NVDAon", side=OrderSide.SELL, path=ExecutionPath.RFQ, avg_price=150.1, notional=20.5, tx_hash=None)
    d = _decision(sig, None, ex, action=Action.SELL)
    txt = templates.explain_decision(d)
    assert "Regime sold NVDAon on its own with real funds" in txt and "via an RFQ quote" in txt and "150.1" in txt
    assert "Exit trigger: the trailing stop was hit." in txt and "Sold position" not in txt


def test_execution_facts_read_defensively_from_dicts_and_extras():
    f = live.execution_facts({"route": "rfq", "amount_usdt": "20", "fill_price": 99.5, "tx_hash": TX, "real_funds": True,
                              "explorer_url": f"https://bscscan.com/tx/{TX}", "priceImpactPercent": "0.31", "status": "FILLED"})
    assert f["route"] == "RFQ" and f["amount_usdt"] == 20 and f["fill_price"] == 99.5 and f["real_funds"] is True
    assert f["explorer_url"] == f"https://bscscan.com/tx/{TX}" and f["price_impact_pct"] == pytest.approx(0.31)
    # quote nested price impact; bogus explorer url is ignored and rebuilt from the hash
    f2 = live.execution_facts({"quote": {"priceImpactPercent": 1.2}, "explorer_url": "https://evil.example/tx", "tx_hash": TX})
    assert f2["price_impact_pct"] == 1.2 and f2["explorer_url"] == f"https://bscscan.com/tx/{TX}"
    # SIM-style fake hash: no link invented
    assert live.execution_facts({"tx_hash": "0xsim123"})["explorer_url"] is None
    assert live.execution_facts(None)["route"] is None


def test_real_funds_flag_marks_live_and_price_impact_is_shown():
    d = _decision(ex=_ex(), mode=TradingMode.LIVE)
    assert live.is_live(d) and live.is_live({"execution": {"real_funds": "true"}}) and not live.is_live({"mode": "SIM"})
    txt = templates.live_execution_sentence(d)
    assert "Price impact unknown." in txt


def test_failed_live_execution_maps_exchange_error_and_keeps_tx():
    ex = _ex(status=OrderStatus.REJECTED, filled_quantity=0, avg_price=None, notional=0,
             error="40375 ONDO_FROM_USD_AMOUNT_TOO_SMALL: Minimum order amount is 20 USD", tx_hash=None)
    txt = templates.explain_decision(_decision(ex=ex))
    assert "was not filled (rejected)" in txt and "minimum order size (Ondo minimum is 20 USD)" in txt
    assert "bought" not in txt and "No position change" in txt


def test_sim_output_unchanged(buy_decision):
    txt = templates.explain_decision(buy_decision)
    assert txt.startswith("Buy NVDAon (auto mode, simulation).") and "LIVE" not in txt


# ------------------------------------------------------------------ risk blocks (Bot 3 bot3/live real strings)
@pytest.mark.parametrize("reason,phrase", [
    ("Blocked (live_daily_loss_cap): Today's LIVE loss 5.20 USDT (realized + unrealized), cap 5.00 USDT",
     "daily loss cap was hit: Regime triggered an emergency stop on its own and is selling all open positions"),
    ("Blocked (live_stale_data): Latest market data is 900s old, limit 180s", "stale"),
    ("Blocked (live_quote): Quote expired at 10:01:02 UTC", "quote expired"),
    ("Blocked (live_slippage): Quote price 183.1 vs reference 180.0: impact 1.72%, slippage tolerance 0.50%; max 1.0% - too much slippage",
     "slippage / price impact is over the cap"),
    ("Blocked (min_order_notional): Order 12.00 USDT, minimum 20.00 USDT (NVDAon token minimum) - below token minimum order",
     "below the token's minimum order size (e.g. 20 USD for Ondo tokens)"),
    ("Blocked (min_order_notional): Order 4.00 USDT, minimum 10.00 USDT - too small", "below the minimum order size"),
    ("Blocked (live_gas_reserve): BNB balance 0.0001, reserve for gas 0.002 - not enough gas", "BNB is left as the gas reserve"),
    ("Blocked (live_wallet_quote): Quote spends 25.10 USDT incl. fee; wallet 20.00 minus buffer 0.50 - insufficient balance",
     "insufficient USDT balance"),
    ("Blocked (live_token_status): NVDAon status is 'offhours' - price is reference only - no LIVE BUY", "reference price"),
    ("Blocked (live_token_status): NVDAon status is 'offhours' - no LIVE BUY", "off-hours"),
    ("Blocked (live_token_status): AAPLon status is 'closed' - no LIVE BUY", "market is closed"),
    ("Blocked (live_token_status): AAPLB status is 'no_liquidity' - no LIVE BUY", "no liquidity"),
    ("Blocked (live_symbol_allowed): TSLAx ends with blocked suffix 'x' (e.g. xStocks: no liquidity on BNB Chain) - not bought in LIVE",
     "xStock tokens are excluded"),
    ("Blocked (trading_mode): LIVE BUYs are disabled: risk config allow_live=False (needs RISK_ALLOW_LIVE, LIVE_TRADING_ENABLED and BINANCE_LIVE_ENABLED all = true)",
     "live trading is not enabled"),
    ("Blocked (wallet_allowed_token): NVDAB is not on the wallet's allowed tokens - the wallet would reject it",
     "not on the Agentic Wallet's allowed-token list"),
    ("Blocked (live_quote_notional): Quote notional 30.00 USDT vs approved size 25.00 USDT (per-order cap 20.00, set by wallet)",
     "set by the Agentic Wallet"),
    ("Blocked (live_quote_notional): Quote notional 30.00 USDT vs approved size 25.00 USDT (per-order cap 25.00, set by risk)",
     "set by Regime's risk limits"),
    ("Blocked (emergency_stop): Emergency stop is active - all new entries are blocked", "emergency stop is active"),
    ("Blocked (max_total_exposure): Total exposure 50 USDT", "max total exposure"),
    ("Blocked (wallet_exit_reserve): keeping 25 USDT of the daily quota to sell all open positions", "kept in reserve"),
])
def test_bot3_block_reasons_are_plain_and_keep_numbers(reason, phrase):
    txt = templates.explain_decision(_blocked(reason))
    assert "Risk blocked the trade, no real funds were spent:" in txt
    assert phrase in txt, txt
    detail = reason.split("): ", 1)[1].replace("approved size", "risk-allowed size")
    if live.has_env_names(detail):  # config / env-var names never reach user text
        assert not live.has_env_names(txt), txt
    else:
        assert detail.rstrip(".") in txt  # exact limit numbers kept
    assert "Blocked (" not in txt and not APPROVAL.search(txt)


def test_block_reasons_from_checks_when_reasons_empty():
    d = _blocked(checks=[RiskCheck(name="live_gas_reserve", passed=False, detail="BNB balance 0, reserve for gas 0.002")])
    assert "gas reserve" in templates.explain_decision(d)


def test_size_cut_names_limit_and_who_set_it():
    sig = _sig()
    v = RiskVerdict(signal_id=sig.id, approved=True, adjusted_size_pct=2.0, adjusted_notional=20.0, checks=[
        RiskCheck(name="live_order_cap", passed=True, detail="LIVE per-order cap 25.00 USDT: reduced from 30.00 to 25.00 USDT"),
        RiskCheck(name="wallet_per_tx_cap", passed=True, detail="Wallet per-transaction limit 20.00 USDT: reduced from 25.00 to 20.00 USDT"),
        RiskCheck(name="wallet_exit_reserve", passed=True, detail="Exit quota reserve: reduced from 20.00 to 15.00 USDT"),
    ])
    txt = templates.explain_decision(_decision(sig, v, None))
    assert "size cut from 30.00 to 25.00 USDT by the per-order cap (set by Regime's risk limits)" in txt
    assert "size cut from 25.00 to 20.00 USDT by the per-transaction limit (set by the Agentic Wallet)" in txt
    assert "size cut from 20.00 to 15.00 USDT by part of the Agentic Wallet's daily allowance is kept in reserve" in txt


# ------------------------------------------------------------------ engine STAY_OUT codes (Bot 2 bot2/live)
@pytest.mark.parametrize("code,phrase", [
    ("live_below_min_order", "minimum order (e.g. 20 USD for Ondo tokens) would exceed the per-order cap"),
    ("live_illiquid_family", "xStock tokens are excluded"),
    ("live_token_paused", "paused or closed"),
    ("insufficient_data", "at least 61 valid bars"),
    ("live_stale_data", "stale"),
    ("live_no_volume", "zero on-chain volume"),
    ("live_thin_liquidity", "liquidity is too thin"),
    ("live_future_data", "in the future"),
    ("live_no_data", "no valid price candles"),
    ("live_price_impact", "which way the price impact goes"),
])
def test_engine_stay_out_codes(code, phrase):
    sig = _sig(action=Action.STAY_OUT, conditions_met=[], conditions_failed=[code])
    v = RiskVerdict(signal_id=sig.id, approved=False, reasons=["Blocked (signal_action): Signal is STAY_OUT - nothing to execute"])
    txt = templates.explain_decision(_decision(sig, v, None, action=Action.STAY_OUT))
    assert "Stayed out because " + live.plain_reason(code) in txt and phrase in txt
    assert code.replace("_", " ") not in txt.split("Stayed out because")[0]


# ------------------------------------------------------------------ plain_reason edge cases
@pytest.mark.parametrize("raw,phrase", [
    ("Binance Web3 API error 40367: market state not tradable", "market is closed"),
    ("40369 market closed for NVDAB", "market is closed"),
    ("40374 insufficient liquidity", "not enough liquidity"),
    ("40401", "quote expired"),
    ("market_closed", "market is closed"),
    ("below_minimum", "minimum order"),
    ("insufficient_liquidity", "not enough liquidity"),
    ("live_token_paused: NVDAB not tradable now (status PAUSED)", "paused"),
    ("per-order cap of 25 USDT exceeded", "per-order cap"),
    ("remaining daily allowance can't cover selling all open positions today", "kept in reserve"),
])
def test_plain_reason_variants(raw, phrase):
    assert phrase in live.plain_reason(raw)


def test_unknown_reasons_pass_through_cleanly():
    assert live.plain_reason("something   new happened.") == "something new happened"
    assert live.plain_reason("Blocked (brand_new_check): limit 3") == "brand new check (limit 3)"
    assert live.plain_reason(None) == "no reason given"
    assert live.plain_reasons(["live_stale_data", "live_stale_data"]) == [live.REASON_TEXT["live_stale_data"]]


# ------------------------------------------------------------------ status / Q&A
def test_live_control_line_and_trade_request():
    st = StatusSnapshot(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False),
                        portfolio=Portfolio(starting_capital=100, cash=100, equity=100, mode=TradingMode.LIVE))
    line = templates.control_line(st)
    assert "autonomously 24/7 with real funds" in line and not APPROVAL.search(line)
    ans = templates.answer("buy NVDAB now", st)
    assert "never place" in ans and "confirm" not in ans


# ------------------------------------------------------------------ Grok path (mocked, offline)
def test_live_prompt_carries_live_rules_and_facts(make_client, reply):
    client, rec = make_client(reply(f"The agent bought NVDAB with real funds, tx https://bscscan.com/tx/{TX}"))
    exp = DefaultExplainer(client)
    out = exp.explain_decision(_decision(ex=_ex()))
    assert exp.last_source == "grok" and out.startswith("The agent bought")
    msgs = rec.payloads[0]["messages"]
    assert "NO human approval" in msgs[0]["content"]
    facts = json.loads(msgs[1]["content"].split("LIVE FACTS JSON:\n", 1)[1].split("\n\nDECISION JSON", 1)[0])
    assert facts["real_funds"] is True and facts["execution"]["explorer_url"] == f"https://bscscan.com/tx/{TX}"
    assert facts["execution"]["route"] == "SWAP" and facts["execution"]["amount_usdt"] == 24.99


def test_live_grok_approval_wording_falls_back_to_template(make_client, reply):
    client, _ = make_client(reply("Waiting for your confirmation before buying NVDAB."))
    exp = DefaultExplainer(client)
    out = exp.explain_decision(_decision(ex=_ex()))
    assert exp.last_source == "template" and "confirmation" in (exp.last_error or "")
    assert out == templates.explain_decision(_decision(ex=_ex())) or out.startswith("LIVE, real funds")


def test_live_grok_missing_tx_link_is_appended(make_client, reply):
    client, _ = make_client(reply("The agent bought NVDAB on its own."))
    out = DefaultExplainer(client).explain_decision(_decision(ex=_ex()))
    assert f"https://bscscan.com/tx/{TX}" in out and out.startswith("The agent bought NVDAB on its own.")


def test_live_blocked_prompt_has_plain_block_reasons(make_client, reply):
    client, rec = make_client(reply("Risk blocked it."))
    DefaultExplainer(client).explain_decision(_blocked("Blocked (live_order_cap): LIVE per-order cap 25.00 USDT"))
    content = rec.payloads[0]["messages"][1]["content"]
    facts = json.loads(content.split("LIVE FACTS JSON:\n", 1)[1].split("\n\nDECISION JSON", 1)[0])
    assert facts["block_reasons"] == ["the per-order cap (set by Regime's risk limits) (LIVE per-order cap 25.00 USDT)"]


def test_live_summary_prompt_is_autonomous(make_client, reply):
    st = StatusSnapshot(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False),
                        portfolio=Portfolio(starting_capital=100, cash=100, equity=100, mode=TradingMode.LIVE))
    client, rec = make_client(reply("ok"))
    DefaultExplainer(client).summarize_state(st)
    assert "NO human approval" in rec.payloads[0]["messages"][0]["content"]


def test_no_key_uses_template_offline():
    d = _decision(ex=_ex())
    exp = DefaultExplainer()
    assert exp.explain_decision(d) == templates.explain_decision(d) and exp.last_source == "template"


# ------------------------------------------------------------------ v2: pending tx, impact unknown, warnings, session
def test_pending_on_chain_has_no_approval_wording():
    ex = _ex(status=OrderStatus("PENDING_CONFIRMATION")) if "PENDING_CONFIRMATION" in OrderStatus.__members__ else None
    f = {"status": "PENDING_CONFIRMATION", "side": "BUY", "symbol": "NVDAB", "route": "SWAP", "amount_usdt": 20.0,
         "tx_hash": TX, "mode": "LIVE"}
    d = _decision(ex=ex) if ex is not None else _decision()
    if ex is None:  # contract has no PENDING_CONFIRMATION yet: exercise the sentence with a dict execution
        d = d.model_copy(update={"execution": f})
    txt = templates.live_execution_sentence(d)
    assert "waiting to settle on BNB Chain" in txt and f"https://bscscan.com/tx/{TX}" in txt
    assert not APPROVAL.search(txt)


def test_price_impact_percent_and_unknown():
    assert "Price impact unknown." in templates.explain_decision(_decision(ex=_ex()))
    d = _decision().model_copy(update={"execution": {"status": "FILLED", "side": "BUY", "symbol": "NVDAB", "route": "SWAP",
                                                     "notional": 20.0, "filled_quantity": 0.1, "avg_price": 200.0,
                                                     "price_impact_pct": 0.35, "tx_hash": TX, "mode": "LIVE"}})
    assert "Price impact 0.35%." in templates.live_execution_sentence(d)


@pytest.mark.parametrize("raw,phrase", [
    ("exit_quota_shortfall", "cannot cover selling all open positions"),
    ("wallet_limits_stale", "more than 15 minutes ago"),
    ("Blocked (wallet_exit_reserve): Keep wallet quota for exits: reduced from 20.00 to 5.00 USDT", "kept in reserve"),
    ("Agentic Wallet not connected: run `baw auth signin` and confirm in the Binance App", "re-scans the QR code"),
    ("wallet session expired", "paused until Rishu re-scans"),
    ("40367", "cooldown"), ("40369 market closed for NVDAB", "cooldown"), ("40374 insufficient liquidity", "cooldown"),
])
def test_v2_codes(raw, phrase):
    assert phrase in live.plain_reason(raw)


def test_slippage_exceeded_code():
    from backend.llm.live import plain_reason
    assert "rejected on-chain" in plain_reason("slippage_exceeded")


def test_bot4_new_codes():
    from backend.llm.live import plain_reason
    assert "maximum" in plain_reason("above_maximum")
    assert "xStock" in plain_reason("xstock_disabled")


def test_baw_session_codes():
    from backend.llm.live import plain_reason
    assert "re-scans the QR" in plain_reason("session_expired")
    assert "QR" in plain_reason("wallet_not_ready")


def test_order_in_flight_code():
    from backend.llm.live import plain_reason
    assert "settling" in plain_reason("order_in_flight")


def test_pending_exit_codes():
    from backend.llm.live import plain_reason
    assert "still settling" in plain_reason("pending_exit")
    assert "still settling" in plain_reason("exit_pending")


def test_exchange_cap_and_lock_codes_are_plain():
    from backend.llm import live
    for raw in ("order_cap_exceeded", "order_cap_exceeded: SELL 550 > 500", "tx_locked"):
        txt = live.plain_reason(raw)
        assert "order_cap_exceeded" not in txt and "tx_locked" not in txt


def test_exit_reasons_are_plain():
    from backend.llm import live
    txt = live.plain_reason("TAKE_PROFIT: high 332.57 reached target 332.20 (entry 296.61); filled at 332.20; PnL +12.00%")
    assert txt.startswith("Take-profit hit:") and "TAKE_PROFIT" not in txt and ";" not in txt and "332.20" in txt
    assert live.plain_reason("STOP_LOSS: price 90.0 below stop 91.0").startswith("Stop-loss hit:")

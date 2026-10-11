"""Regression tests for the Bot 5 strategy x explanation sweep findings (F1-F5, F7, P1, P2) plus the small-wallet /
gas hints, the default entry rule and the 'size raised to the token minimum' wording. Offline (templates / mocked Grok)."""
import json
import re

import pytest

from backend.contracts import (
    Action, AgentMode, Alert, AlertLevel, ConditionLogic, ControlState, Decision, ExecutionPath, ExecutionResult,
    ExitReason, ManualStrategy, OrderSide, OrderStatus, Portfolio, PresetName, Regime, RiskCheck, RiskVerdict, Signal,
    StatusSnapshot, TradingMode,
)
from backend.llm import DefaultExplainer, live, prompts, templates
from backend.llm.parser import DEFAULT_ENTRY_NOTE, keyword_parse, keyword_parse_with_notes, validate_llm_strategy

TX = "0x" + "cd34" * 16
APPROVAL = re.compile(r"confirm|approv|await|waiting for your|your OK", re.I)
SENT = re.compile(r"\bsubmitted\b|\bon its own\b|\bautonomous", re.I)
SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
ENGINE_REASON = ("Regime SIDEWAYS (Flat SMA50 (slope -0.19%) and low volatility (0.59%)); SIDEWAYS + ACTIVE -> GRID "
                 "(preferred preset). Grid: price in the bottom grid cell 1/6 of range 229.2-234.1 "
                 "[live-safe size 15% x0.25 capped at 2% -> 2% = 20.00 USDT]")


@pytest.fixture(autouse=True)
def _reset_limits():
    templates.set_live_limits(None)
    yield
    templates.set_live_limits(None)


def _sig(**kw):
    base = dict(symbol="NVDAB", action=Action.BUY, source=AgentMode.AUTO, strategy="GRID", preset=PresetName.GRID,
                regime=Regime.SIDEWAYS, reason=ENGINE_REASON, conditions_met=["bottom_cell"], price=229.4252,
                proposed_size_pct=2.0, proposed_notional=20.0)
    base.update(kw)
    return Signal(**base)


def _verdict(sig, approved=True, conf=False, reasons=(), checks=()):
    return RiskVerdict(signal_id=sig.id, approved=approved, adjusted_size_pct=2.0 if approved else 0.0,
                       adjusted_notional=20.0 if approved else 0.0, requires_confirmation=conf, reasons=list(reasons),
                       checks=list(checks))


def _ex(mode=TradingMode.LIVE, **kw):
    base = dict(client_order_id="c1", symbol="NVDAB", side=OrderSide.BUY, status=OrderStatus.FILLED,
                path=ExecutionPath.SWAP, mode=mode, filled_quantity=0.0872, avg_price=229.4, notional=20.0, fee=0.02,
                tx_hash=TX if mode == TradingMode.LIVE else None)
    base.update(kw)
    return ExecutionResult(**base)


def _d(sig=None, v=None, ex=None, mode=TradingMode.LIVE, action=None, reason=None):
    sig = sig or _sig()
    return Decision(mode=mode, agent_mode=AgentMode.AUTO, symbol=sig.symbol, regime=sig.regime, strategy=sig.strategy,
                    signal=sig, action=action or sig.action, risk_verdict=v, execution=ex,
                    reason=sig.reason if reason is None else reason)


def _awaiting_loop_shape(mode=TradingMode.LIVE):
    """Exactly what backend/app/loop.py _pending_decision builds (confirmation mode, opt-in)."""
    sig = _sig()
    v = _verdict(sig, conf=True, reasons=["Approved 20.00 USDT - awaiting user confirmation"])
    d0 = _d(sig, v, None, mode)
    ex = ExecutionResult(client_order_id=d0.id, symbol="NVDAB", side=OrderSide.BUY, status=OrderStatus.PENDING_CONFIRMATION,
                         path=ExecutionPath.SWAP, mode=mode, notional=20.0)
    return d0.model_copy(update={"execution": ex, "reason": f"awaiting user confirmation: {sig.reason}"})


def _status(mode, conf, last=None, alerts=None):
    return StatusSnapshot(control=ControlState(trading_mode=mode, confirmation_mode=conf),
                          portfolio=Portfolio(starting_capital=1000, cash=980, equity=1000, mode=mode),
                          agent_mode=AgentMode.AUTO, active_symbol="NVDAB", current_regime=Regime.SIDEWAYS,
                          active_strategy="SMART_DCA", last_decision=last)


# ------------------------------------------------------------------ F1: LIVE + confirmation mode (opt-in)
def test_f1_live_awaiting_confirmation_is_honest_and_not_sent():
    txt = templates.explain_decision(_awaiting_loop_shape())
    assert "waiting for your OK in the dashboard" in txt and "nothing was sent" in txt and "no real funds moved" in txt
    assert not SENT.search(txt), txt
    assert "waiting to settle" not in txt and "awaiting user confirmation" not in txt
    assert txt.startswith("LIVE, real funds, confirmation mode: Buy NVDAB (AUTO mode), proposed.")


def test_f1_requires_confirmation_without_execution_is_awaiting():
    sig = _sig()
    d = _d(sig, _verdict(sig, conf=True), None)
    assert templates.awaiting_confirmation(d)
    txt = templates.explain_decision(d)
    assert templates.AWAITING_SENTENCE in txt and "placing this order on its own" not in txt


def test_f1_filled_after_user_ok_drops_on_its_own():
    sig = _sig()
    txt = templates.explain_decision(_d(sig, _verdict(sig, conf=True), _ex()))
    assert "Regime bought NVDAB after your OK with real funds" in txt and "on its own" not in txt
    assert "confirmation mode" in txt and f"https://bscscan.com/tx/{TX}" in txt


def test_f1_autonomous_live_keeps_autonomous_wording_and_chain_pending_still_settling():
    sig = _sig()
    txt = templates.explain_decision(_d(sig, _verdict(sig), _ex()))
    assert txt.startswith("LIVE, real funds, autonomous:") and "on its own" in txt and not APPROVAL.search(txt)
    pend = _ex(status=OrderStatus.PENDING_CONFIRMATION, filled_quantity=0.0, avg_price=None)
    t2 = templates.explain_decision(_d(sig, _verdict(sig), pend))
    assert "waiting to settle on BNB Chain" in t2 and not APPROVAL.search(t2)


def test_f1_status_line_driven_by_confirmation_mode():
    on = templates.control_line(_status(TradingMode.LIVE, True))
    off = templates.control_line(_status(TradingMode.LIVE, False))
    assert "autonomously" not in on and "waits for your OK in the dashboard" in on
    assert "autonomously 24/7 with real funds" in off and not APPROVAL.search(off)
    assert "confirm /" in templates.answer("buy NVDAB now", _status(TradingMode.LIVE, True))
    assert "confirm" not in templates.answer("buy NVDAB now", _status(TradingMode.LIVE, False))


def test_f1_grok_guard_and_prompt_in_confirmation_mode(make_client, reply):
    d = _awaiting_loop_shape()
    client, rec = make_client(reply("The agent submitted a buy of NVDAB on its own."))
    exp = DefaultExplainer(client)
    assert exp.explain_decision(d) == templates.explain_decision(d) and exp.last_source == "template"
    assert "unconfirmed" in (exp.last_error or "")
    sys_msg = rec.payloads[0]["messages"][0]["content"]
    assert "confirmation mode ON" in sys_msg and "NO human approval" not in sys_msg
    facts = json.loads(rec.payloads[0]["messages"][1]["content"].split("LIVE FACTS JSON:\n", 1)[1].split("\n\nDECISION JSON", 1)[0])
    assert facts["awaiting_user_ok"] is True and facts["autonomous"] is False and facts["strategy"] == "Grid"
    honest = "The agent proposed a 20 USDT Grid buy of NVDAB; it is waiting for your OK in the dashboard. Nothing was sent."
    client2, _ = make_client(reply(honest))
    exp2 = DefaultExplainer(client2)
    assert exp2.explain_decision(d) == honest and exp2.last_source == "grok"


def test_f1_summary_prompt_uses_confirmation_rules(make_client, reply):
    client, rec = make_client(reply("ok"))
    DefaultExplainer(client).summarize_state(_status(TradingMode.LIVE, True))
    assert "confirmation mode ON" in rec.payloads[0]["messages"][0]["content"]


# ------------------------------------------------------------------ F7: 'confirmed' is not approval wording
def test_f7_confirmed_uptrend_is_not_approval_wording(make_client, reply):
    from backend.llm.explainer import _APPROVAL_WORDING
    assert not _APPROVAL_WORDING.search("Trend: no confirmed uptrend, so the agent stayed out.")
    assert _APPROVAL_WORDING.search("Waiting for your confirmation") and _APPROVAL_WORDING.search("awaiting the user")
    sig = _sig(action=Action.STAY_OUT, strategy="TREND_FOLLOWING", preset=PresetName.TREND_FOLLOWING)
    client, _ = make_client(reply("The agent stayed out of NVDAB: no confirmed uptrend."))
    exp = DefaultExplainer(client)
    exp.explain_decision(_d(sig, None, None))
    assert exp.last_source == "grok"


# ------------------------------------------------------------------ F2: readable preset names
@pytest.mark.parametrize("code,label", [("DCA", "DCA"), ("SMART_DCA", "Smart DCA"), ("BUY_THE_DIP", "Buy the Dip"),
                                        ("TREND_FOLLOWING", "Trend following"), ("GRID", "Grid"),
                                        ("NONE", "no preset (staying out)")])
@pytest.mark.parametrize("mode", [TradingMode.SIM, TradingMode.LIVE])
def test_f2_preset_labels(code, label, mode):
    sig = _sig(strategy=code, preset=None if code == "NONE" else PresetName(code), action=Action.STAY_OUT, reason="x")
    txt = templates.explain_decision(_d(sig, None, None, mode))
    assert f"Strategy: {label}" in txt and code not in SNAKE.findall(txt)
    assert templates.strategy_label("MANUAL:My RSI") == 'manual rule set "My RSI"'


# ------------------------------------------------------------------ F3: engine / risk reasons in plain words
def test_f3_engine_reason_translated():
    t = templates.humanize_reason("Regime STRONG_UPTREND (Price 10.2% above a rising SMA50); STRONG_UPTREND + PATIENT "
                                  "-> TREND_FOLLOWING (fallback (preferred SMART_DCA is disabled)). Trend: no confirmed "
                                  "uptrend (macd_above_signal failed)")
    assert t.startswith("Market regime strong uptrend") and "with the Patient style selects Trend following" in t
    assert "preferred Smart DCA is disabled" in t and "MACD above its signal line failed" in t and not SNAKE.search(t)
    assert templates.humanize_reason("TAKE_PROFIT: high 101.97 reached target 93.48").startswith("Take-profit, high 101.97")
    assert "with the Balanced style: policy is to pause" in templates.humanize_reason(
        "Regime HIGH_VOLATILITY (x); HIGH_VOLATILITY + BALANCED: policy is to pause / stay out")


LIVE_BLOCK = ("Blocked (trading_mode): LIVE BUYs are disabled: risk config allow_live=False (needs RISK_ALLOW_LIVE, "
              "LIVE_TRADING_ENABLED and BINANCE_LIVE_ENABLED all = true)")


def test_f3_live_block_not_repeated_no_env_names_short():
    sig = _sig(strategy="DCA", preset=PresetName.DCA, reason="Regime STRONG_UPTREND (up); STRONG_UPTREND + PATIENT -> "
               "DCA (preferred preset). DCA: scheduled buy (every 24 bars)", conditions_met=["dca_schedule"])
    reasons = [LIVE_BLOCK, "Blocked (live_stale_data): No price/bar timestamp supplied - cannot prove market data is fresh"]
    v = _verdict(sig, approved=False, reasons=reasons)
    d = _d(sig, v, None, action=Action.STAY_OUT, reason="BUY not executed: " + "; ".join(reasons))
    for txt in (templates.explain_decision(d), templates.explain_decision(d.model_copy(update={"mode": TradingMode.SIM}))):
        assert "Blocked (" not in txt and not live.has_env_names(txt) and "allow_live" not in txt
        assert txt.count("live trading is not enabled") == 1 and "not executed" not in txt
        assert "scheduled buy (every 24 bars)" in txt and not SNAKE.search(txt), txt
        assert len(txt) < 800


def test_f3_risk_exit_reason_plain():
    sig = _sig(action=Action.SELL, strategy="MANUAL:R", preset=None, exit_reason=ExitReason.TAKE_PROFIT,
               position_id="75b28244aaaa", reason="TAKE_PROFIT: high 101.9712 reached target 93.4797",
               conditions_met=["TAKE_PROFIT"], price=101.83)
    txt = templates.explain_decision(_d(sig, None, _ex(TradingMode.SIM, side=OrderSide.SELL, avg_price=101.83), TradingMode.SIM))
    assert "Reason: Take-profit, high 101.9712" in txt and "Conditions met: take-profit." in txt and not SNAKE.search(txt)


# ------------------------------------------------------------------ F4: 'waiting' only when nothing executed
def test_f4_filled_after_confirmation_does_not_say_waiting_and_status_words():
    sig = _sig()
    txt = templates.explain_decision(_d(sig, _verdict(sig, conf=True), _ex(TradingMode.SIM), TradingMode.SIM))
    assert "waiting" not in txt and "Execution: filled via a swap" in txt
    pend = templates.explain_decision(_awaiting_loop_shape(TradingMode.SIM))
    assert "waiting for your confirmation before executing" in pend and "PENDING_CONFIRMATION" not in pend
    exp = templates.explain_decision(_d(sig, _verdict(sig), _ex(TradingMode.SIM, status=OrderStatus.EXPIRED,
                                                                filled_quantity=0.0), TradingMode.SIM))
    assert "Execution: expired via a swap" in exp and "EXPIRED" not in exp


# ------------------------------------------------------------------ F5: same code translation in every mode
@pytest.mark.parametrize("mode", [TradingMode.SIM, TradingMode.PAPER, TradingMode.LIVE])
def test_f5_engine_live_codes_translated_in_every_mode(mode):
    sig = _sig(symbol="TSLAx", action=Action.STAY_OUT, conditions_failed=["live_illiquid_family"],
               reason="Live safety blocked entry: TSLAx is a xstock token: no usable on-chain liquidity on BSC")
    txt = templates.explain_decision(_d(sig, None, None, mode))
    assert "Stayed out because xStock tokens are excluded in live trading" in txt
    assert "live illiquid family" not in txt and "live_illiquid_family" not in txt


# ------------------------------------------------------------------ summary / Q&A codes
def test_summary_and_answer_map_status_and_alert_codes():
    st = _status(TradingMode.SIM, True, last=_awaiting_loop_shape(TradingMode.SIM))
    alerts = [Alert(level=AlertLevel.WARNING, code="RISK_BLOCKED", message="BUY blocked: min order", symbol="NVDAB")]
    for txt in (templates.summarize_state(st, None, alerts), templates.answer("why did you buy?", st),
                templates.answer("any alerts?", st, None, alerts)):
        assert "PENDING_CONFIRMATION" not in txt and "RISK_BLOCKED" not in txt and "SMART_DCA" not in txt
    assert "[WARNING] risk blocked a trade (NVDAB)" in templates.answer("any alerts?", st, None, alerts)
    assert "Active strategy: Smart DCA" in templates.summarize_state(st)


# ------------------------------------------------------------------ small wallet / gas hints (LIVE)
def _min_order_block():
    sig = _sig(symbol="NVDAon")
    v = _verdict(sig, approved=False, reasons=["Blocked (min_order_notional): Order 6.25 USDT, minimum 10.00 USDT - too small"])
    return _d(sig, v, None, action=Action.STAY_OUT, reason="BUY not executed: x")


def test_small_wallet_hint_uses_min_wallet_usdt_when_available():
    d = _min_order_block()
    assert "Small wallet" not in templates.explain_decision(d)  # value unknown -> no guess
    templates.set_live_limits({"min_wallet_usdt": "70"})
    txt = templates.explain_decision(d)
    assert "the wallet likely needs about 70 USDT or more" in txt
    templates.set_live_limits({"min_wallet_usdt": None, "junk": object()})  # read defensively
    assert "Small wallet" not in templates.explain_decision(d)
    sig = _sig()
    v = _verdict(sig, approved=False, reasons=["Blocked (min_order_notional): Order 4.00 USDT below min order; "
                                               "min wallet 72 USDT"])
    assert "about 72 USDT" in templates.explain_decision(_d(sig, v, None, action=Action.STAY_OUT, reason="BUY not executed: x"))


def test_small_wallet_hint_from_exchange_error_and_explainer_setter():
    DefaultExplainer().set_live_limits({"min_wallet_usdt": 70})
    sig = _sig(symbol="NVDAon")
    ex = _ex(symbol="NVDAon", status=OrderStatus.REJECTED, filled_quantity=0.0, avg_price=None, tx_hash=None,
             error="40375 Minimum order amount is 20 USD")
    assert "about 70 USDT or more" in templates.explain_decision(_d(sig, _verdict(sig), ex))


def test_gas_hint():
    sig = _sig()
    v = _verdict(sig, approved=False, reasons=["Blocked (live_gas_reserve): BNB balance 0.0001, reserve for gas 0.002 - not enough gas"])
    d = _d(sig, v, None, action=Action.STAY_OUT, reason="BUY not executed: x")
    assert "Keep about 0.005 BNB in the wallet for gas (the hard reserve is 0.002 BNB)." in templates.explain_decision(d)
    templates.set_live_limits({"bnb_gas_reserve": 0.003})
    assert "the hard reserve is 0.003 BNB" in templates.explain_decision(d)
    ex = _ex(status=OrderStatus.FAILED, filled_quantity=0.0, tx_hash=None, error="insufficient_gas")
    assert "0.005 BNB" in templates.explain_decision(_d(sig, _verdict(sig), ex))


# ------------------------------------------------------------------ size raised to the token minimum
def test_size_raised_to_token_minimum_reads_naturally():
    sig = _sig(reason="Grid: bottom cell [live-safe size 15% x0.25 capped at 2%, bumped 2.50 -> ondo min order 20 USDT "
                      "-> 2% = 20.00 USDT]")
    txt = templates.explain_decision(_d(sig, _verdict(sig), _ex()))
    assert "size raised to the token minimum (20.00 USDT)" in txt and "bumped" not in txt
    v = _verdict(sig, checks=[RiskCheck(name="min_order_notional", passed=True, detail="Order raised from 6.25 to 10.00 USDT (min order)")])
    for mode in (TradingMode.SIM, TradingMode.LIVE):
        t = templates.explain_decision(_d(sig, v, _ex(mode), mode))
        assert "size raised to the token minimum (10.00 USDT)" in t, t
    assert templates.humanize_reason("size increased to min_order_usdt 10 USDT") == "size raised to the token minimum (10 USDT)"


# ------------------------------------------------------------------ parser: P1, P2, 'goes above', default entry
def test_p1_trailing_stop_before_stop_loss():
    for text in ("Buy when RSI below 30, trailing stop 2.5%, stop loss 5%", "Buy when RSI below 30, stop loss 5%, trailing stop 2.5%",
                 "Buy when RSI below 30, 2.5% trailing stop, stop loss 5%"):
        s = keyword_parse(text)
        assert s.exit.stop_loss_pct == 5 and s.exit.trailing_stop.enabled and s.exit.trailing_stop.pct == 2.5, text
    assert keyword_parse("Buy when RSI below 30 with a 3% trailing stop").exit.stop_loss_pct == 6  # default kept


def test_p2_plain_or_between_conditions():
    assert keyword_parse("Buy when RSI below 30 or price above SMA 50").entry.logic == ConditionLogic.OR
    assert keyword_parse("Buy when RSI below 30 or price below lower bollinger band, sell at 10% profit or 5% stop").entry.logic == ConditionLogic.OR
    assert keyword_parse("Buy when RSI below 30 and price above SMA 50, take profit 10% or trailing stop").entry.logic == ConditionLogic.AND


def test_goes_above_rsi_exit_and_goes_below_entry():
    s = keyword_parse("Buy when RSI goes below 28, exit when RSI goes above 80")
    assert s.entry.rsi_below.threshold == 28 and s.exit.rsi_above.enabled and s.exit.rsi_above.threshold == 80


def test_default_entry_is_ema20_with_note():
    s, notes = keyword_parse_with_notes("Trade NVDA with take profit 10% and stop loss 4%")
    enabled = [k for k, v in s.entry.model_dump().items() if isinstance(v, dict) and v.get("enabled")]
    assert enabled == ["price_above_ema"] and s.entry.price_above_ema.period == 20 and notes[0] == DEFAULT_ENTRY_NOTE
    assert s.exit.take_profit_pct == 10 and s.exit.stop_loss_pct == 4
    _, notes2 = keyword_parse_with_notes("Buy when RSI below 30")
    assert DEFAULT_ENTRY_NOTE not in notes2
    exp = DefaultExplainer()
    out, n = exp.parse_strategy_with_notes("take profit 8%")
    assert isinstance(out, ManualStrategy) and out.entry.price_above_ema.enabled and n[0] == DEFAULT_ENTRY_NOTE
    assert exp.last_parse_notes[0] == DEFAULT_ENTRY_NOTE


def test_default_entry_on_llm_path(make_client, reply):
    raw = json.dumps({"kind": "manual", "strategy": {"symbol": "NVDAon", "exit": {"take_profit_pct": 9}}})
    s = validate_llm_strategy(raw, "take profit 9% on nvidia")
    assert s.entry.price_above_ema.enabled and not s.entry.rsi_below.enabled and not s.entry.price_above_sma.enabled
    client, _ = make_client(reply(raw))
    exp = DefaultExplainer(client)
    out = exp.parse_strategy_from_text("take profit 9% on nvidia")
    assert exp.last_source == "grok" and out.entry.price_above_ema.period == 20 and exp.last_parse_notes[0] == DEFAULT_ENTRY_NOTE
    raw2 = json.dumps({"kind": "manual", "strategy": {"entry": {"rsi_below": {"enabled": True, "threshold": 30}}}})
    client2, _ = make_client(reply(raw2))
    exp2 = DefaultExplainer(client2)
    assert exp2.parse_strategy_from_text("buy when RSI below 30").entry.rsi_below.enabled and DEFAULT_ENTRY_NOTE not in exp2.last_parse_notes

from backend.contracts import Action, AgentMode, ControlState, Decision, TradingMode
from backend.llm import templates


def test_explain_buy_mentions_everything(buy_decision):
    txt = templates.explain_decision(buy_decision)
    assert txt.startswith("Buy NVDAon (AUTO mode, SIM).")
    for s in ["Trend following", "strong uptrend", "price above its EMA", "MACD crossed above its signal line",
              "volume above average", "Risk check passed", "filled", "SWAP", "$997.50"]:
        assert s in txt, s


def test_explain_blocked(blocked_decision):
    txt = templates.explain_decision(blocked_decision)
    assert txt.startswith("Stay out of TSLAx (MANUAL mode")
    assert "Risk layer blocked the trade: cooldown active (2 of 5 bars)." in txt and "high volatility" in txt


def test_explain_minimal_and_pending_confirmation(buy_decision):
    d = Decision(mode=TradingMode.PAPER, agent_mode=AgentMode.MANUAL, symbol="AAPLB", strategy="MANUAL:x", action=Action.SELL, reason="")
    txt = templates.explain_decision(d)
    assert "Sell AAPLB" in txt and "Not executed yet." in txt
    v = buy_decision.risk_verdict.model_copy(update={"requires_confirmation": True})
    d2 = buy_decision.model_copy(update={"risk_verdict": v, "execution": None})
    assert "waiting for your confirmation" in templates.explain_decision(d2)


def test_explain_is_deterministic(buy_decision):
    assert templates.explain_decision(buy_decision) == templates.explain_decision(buy_decision)


def test_summary(status, empty_status):
    s = templates.summarize_state(status)
    for part in ["running in SIM mode", "strong uptrend", "Trend following", "$10,052.50", "+0.53%", "NVDAon #pos0001a: 7 @ 142.5", "Last decision"]:
        assert part in s, part
    e = templates.summarize_state(empty_status)
    assert "paused" in e and "user pause" in e and "No open positions." in e and "No decisions yet." in e
    assert "No market regime detected yet" in e


def test_emergency_stop_and_auto_execute_wording(empty_status):
    st = empty_status.model_copy(update={"control": ControlState(emergency_stopped=True, confirmation_mode=False)})
    line = templates.control_line(st)
    assert "EMERGENCY STOPPED" in line and "auto-execute" in line


def test_answer_routes_intents(status):
    assert "strong uptrend" in templates.answer("What's the market regime?", status)
    assert "1 open position" in templates.answer("what positions do I hold", status)
    assert "realized PnL $120.00" in templates.answer("How is my PnL?", status)
    assert templates.answer("why did it do that?", status).startswith("Last decision:")
    assert "Active strategy: Trend following" in templates.answer("which strategy is active", status)
    assert "running in SIM" in templates.answer("is it paused?", status)
    assert templates.answer("tell me a joke", status) == templates.summarize_state(status)


def test_answer_refuses_trade_requests(status):
    a = templates.answer("buy 10 TSLAx now", status)
    assert "never place" in a
    assert "never place" in templates.answer("Please sell my position for me", status)

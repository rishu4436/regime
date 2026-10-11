"""Contracts v0.2.0: Signal.exit_reason / position_id / price (intended fill), Position.id, PerformanceSummary, Alert."""
import json

import pytest

from backend.contracts import Action, ExitReason, OrderStatus
from backend.llm import DefaultExplainer, templates


# ------------------------------------------------------------------ exits in explain_decision (templates)
def test_filled_trailing_stop_exit(sell_filled):
    txt = templates.explain_decision(sell_filled)
    assert "Sold position pos0001a of NVDAon at 153.6 because the trailing stop was hit." in txt
    assert "Price at decision" not in txt  # replaced by the exit sentence
    assert "intended fill" not in txt  # filled exactly at the intended level


@pytest.mark.parametrize("reason,phrase", [
    (ExitReason.TAKE_PROFIT, "the take-profit target was reached"),
    (ExitReason.STOP_LOSS, "the stop-loss was hit"),
    (ExitReason.RSI_EXIT, "RSI rose above the exit level"),
    (ExitReason.MACD_EXIT, "MACD crossed below its signal line"),
    (ExitReason.END, "the backtest ended"),
    (ExitReason.MAX_LOSS, "the max-loss-per-trade limit was reached"),
    (ExitReason.MANUAL, "you closed it manually"),
    (ExitReason.EMERGENCY_STOP, "the emergency stop closed it"),
])
def test_every_exit_reason_has_plain_text(make_sell, reason, phrase):
    txt = templates.explain_decision(make_sell({"exit_reason": reason}, {}))
    assert f"because {phrase}." in txt
    assert set(templates.EXIT_REASON_TEXT) == set(ExitReason)


def test_gap_fill_differs_from_intended(make_sell):
    d = make_sell({"exit_reason": ExitReason.STOP_LOSS, "price": 131.6}, {"avg_price": 128.0})
    assert "Sold position pos0001a of NVDAon at 128 because the stop-loss was hit (intended fill 131.6)." in templates.explain_decision(d)


def test_partial_fill(make_sell):
    d = make_sell(None, {"status": OrderStatus.PARTIALLY_FILLED, "filled_quantity": 3.0})
    assert "partially filled." in templates.explain_decision(d)


def test_pending_exit_uses_intended_fill(make_sell):
    d = make_sell({"exit_reason": ExitReason.TAKE_PROFIT, "price": 156.8})
    txt = templates.explain_decision(d)
    assert "Selling position pos0001a of NVDAon at an intended fill of 156.8 because the take-profit target was reached." in txt
    assert "Not executed yet." in txt


def test_failed_execution_is_not_reported_as_sold(make_sell):
    d = make_sell(None, {"status": OrderStatus.EXPIRED, "filled_quantity": 0.0, "avg_price": None, "error": "quote expired"})
    txt = templates.explain_decision(d)
    assert "Sold" not in txt and "Selling position pos0001a" in txt and "EXPIRED" in txt and "quote expired" in txt


def test_blocked_exit(make_sell):
    d = make_sell(None, None, action=Action.STAY_OUT, approved=False)
    txt = templates.explain_decision(d)
    assert "Wanted to sell position pos0001a of NVDAon at 153.6 because the trailing stop was hit, but it was not executed." in txt
    assert "Risk layer blocked the trade: emergency stop active." in txt


def test_exit_without_position_id_or_reason(make_sell):
    d = make_sell({"position_id": None, "exit_reason": None, "price": None}, {"avg_price": 150.0})
    assert "Sold the NVDAon position at 150." in templates.explain_decision(d)


def test_buy_decision_unchanged(buy_decision):
    txt = templates.explain_decision(buy_decision)
    assert "Sold" not in txt and "Wanted to sell" not in txt and "Price at decision: 142.5." in txt


def test_grok_prompt_carries_exit_fields(make_client, reply, sell_filled):
    client, rec = make_client(reply("ok"))
    DefaultExplainer(client).explain_decision(sell_filled)
    content = rec.payloads[0]["messages"][1]["content"]
    assert "exit_reason" in content and "position_id" in content and "intended fill" in content
    decision = json.loads(content.split("DECISION JSON:\n", 1)[1])
    assert decision["signal"]["exit_reason"] == "TRAILING_STOP"
    assert decision["signal"]["position_id"] == "pos0001aaaabbbb" and decision["signal"]["price"] == 153.6
    assert decision["execution"]["avg_price"] == 153.6


# ------------------------------------------------------------------ PerformanceSummary + alerts
def test_summary_with_performance_and_alerts(status, perf, alerts):
    s = templates.summarize_state(status, perf, alerts)
    assert "Performance: equity $10,452.50 (+4.53% vs start $10,000.00)" in s
    assert "max drawdown 3.21%" in s and "win rate 62.5% over 8 closed trade(s) (5 won, 3 lost)" in s
    assert "2 loss(es) in a row" in s
    assert "Recent alerts (+1 more): [CRITICAL] DAILY_LOSS: daily loss limit reached; trading paused;" in s
    assert s.index("DAILY_LOSS") < s.index("CONSECUTIVE_LOSSES") < s.index("QUOTE_EXPIRED (TSLAx)")
    assert "old info" not in s  # only the 3 most recent


def test_summary_without_extras_is_backward_compatible(status):
    s = templates.summarize_state(status)
    assert "alerts" not in s.lower() and "Equity $10,052.50" in s
    assert templates.summarize_state(status, alerts=[]).endswith("No recent alerts.")


def test_answer_uses_performance_and_alerts(status, perf, alerts):
    assert "max drawdown 3.21%" in templates.answer("what's my drawdown?", status, perf)
    assert "win rate 62.5%" in templates.answer("what is the win rate", status, perf)
    a = templates.answer("any alerts or problems?", status, perf, alerts)
    assert a.startswith("Recent alerts") and "DAILY_LOSS" in a
    assert templates.answer("any warnings?", status) == "No recent alerts."
    assert "Performance:" in templates.answer("hmm", status, perf, alerts) and "Recent alerts" in templates.answer("hmm", status, perf, alerts)


def test_explainer_fallback_passes_extras(status, perf, alerts):
    ex = DefaultExplainer()
    assert ex.summarize_state(status, performance=perf, alerts=alerts) == templates.summarize_state(status, perf, alerts)
    assert ex.answer("alerts?", status, alerts=alerts) == templates.answer("alerts?", status, None, alerts)


def test_grok_prompts_include_performance_and_alerts(make_client, reply, status, perf, alerts):
    client, rec = make_client(reply("ok"))
    ex = DefaultExplainer(client)
    ex.summarize_state(status, performance=perf, alerts=alerts)
    ex.answer("how risky is it?", status, performance=perf, alerts=alerts)
    ex.summarize_state(status)
    for payload in rec.payloads[:2]:
        content = payload["messages"][1]["content"]
        p = json.loads(content.split("PERFORMANCE JSON:\n", 1)[1].split("\n\n", 1)[0])
        assert p["max_drawdown_pct"] == 3.21 and len(p["equity_curve"]) == 20  # curve trimmed
        al = json.loads(content.split("RECENT ALERTS JSON (oldest first):\n", 1)[1].split("\n\n", 1)[0])
        assert [a["code"] for a in al][-1] == "DAILY_LOSS" and len(al) == 4
    plain = rec.payloads[2]["messages"][1]["content"]
    assert "PERFORMANCE JSON" not in plain and "ALERTS JSON" not in plain


def test_position_id_shown_in_positions(status):
    assert "NVDAon #pos0001a: 7 @ 142.5" in templates.positions_line(status)

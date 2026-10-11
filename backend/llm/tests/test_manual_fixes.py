"""Manual-mode SIM test findings on 3f13ae2 (Bot 5): volume entry phrasing, unparsed-indicator notes, RSI-exit default
note, plain mode / route / exit-reason wording, and the pending-approval reason without raw action codes or ids.
Offline (keyword parser, templates, mocked Grok)."""
import json
import re

import pytest

from backend.contracts import (
    Action, AgentMode, Decision, ExecutionPath, ExecutionResult, ExitReason, ManualStrategy, OrderSide, OrderStatus,
    PresetName, Regime, Signal, TradingMode,
)
from backend.llm import DefaultExplainer, templates
from backend.llm.parser import (DEFAULT_ENTRY_NOTE, RSI_EXIT_NOTE, keyword_parse_with_notes, text_has_entry_rule,
                                validate_llm_strategy)

ENTRY = ("rsi_below", "price_above_sma", "price_above_ema", "macd_bullish_cross", "bollinger_lower", "volume_above_avg",
         "breakout")
CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b(?:SIM|PAPER|SWAP|RFQ|BUY|SELL)\b|\b[0-9a-f]{12,}\b")
RSI_NOTE = RSI_EXIT_NOTE.format(thr=70)


def _on(s: ManualStrategy) -> list[str]:
    return [n for n in ENTRY if getattr(s.entry, n).enabled]


# ------------------------------------------------------------------ 1. volume entry + unparsed notes
@pytest.mark.parametrize("phrase", [
    "volume is above average", "volume is high", "volume goes above average", "volume is above its average",
    "volume higher than average", "volume is running above average", "volume above average", "volume spike",
    "volume surge", "high volume", "above-average volume", "above average volume", "volume > average",
])
def test_volume_phrasings_give_volume_entry(phrase):
    s, notes = keyword_parse_with_notes(f"buy NVDAB when {phrase}, take profit 5%")
    assert _on(s) == ["volume_above_avg"] and DEFAULT_ENTRY_NOTE not in notes
    assert not any(n.startswith("Couldn't understand") for n in notes)


def test_case2_macd_and_volume_trailing():
    s, notes = keyword_parse_with_notes("buy when MACD crosses above signal and volume is above average, trailing stop 3%",
                                        "NVDAB")
    assert _on(s) == ["macd_bullish_cross", "volume_above_avg"] and s.entry.logic.value == "AND"
    assert s.exit.trailing_stop.enabled and s.exit.trailing_stop.pct == 3
    assert DEFAULT_ENTRY_NOTE not in notes and not any("Couldn't" in n for n in notes)


def test_volume_only_is_not_ema_default():
    s, notes = keyword_parse_with_notes("buy when volume is above average, trailing stop 3%", "NVDAB")
    assert _on(s) == ["volume_above_avg"] and DEFAULT_ENTRY_NOTE not in notes
    assert text_has_entry_rule("buy when volume is above average, trailing stop 3%")


def test_min_volume_filter_is_not_an_entry():
    s, notes = keyword_parse_with_notes("buy when price is above EMA 20, minimum volume 1000, take profit 8%")
    assert _on(s) == ["price_above_ema"] and s.filters.min_volume.enabled
    assert not any("Couldn't" in n for n in notes)


def test_unrecognised_indicator_phrase_gets_a_note():
    s, notes = keyword_parse_with_notes("buy when volume is weird and price above EMA 20, take profit 5%")
    assert _on(s) == ["price_above_ema"]
    assert 'Couldn\'t understand "volume is weird"; it was left out.' in notes


def test_no_note_for_disabled_rsi_exit_phrase():
    _, notes = keyword_parse_with_notes("buy when MACD crosses above signal, no rsi exit")
    assert not any("Couldn't" in n for n in notes) and RSI_NOTE not in notes


def test_llm_path_keeps_volume_the_model_dropped(make_client, reply):
    text = "buy when MACD crosses above signal and volume is above average, trailing stop 3%"
    raw = json.dumps({"kind": "manual", "strategy": {"symbol": "NVDAB", "entry": {
        "macd_bullish_cross": {"enabled": True}, "volume_above_avg": {"enabled": False}},
        "exit": {"trailing_stop": {"enabled": True, "pct": 3}}}})
    s = validate_llm_strategy(raw, text)
    assert {"macd_bullish_cross", "volume_above_avg"} <= set(_on(s))
    client, _ = make_client(reply(raw))
    exp = DefaultExplainer(client)
    out, notes = exp.parse_strategy_with_notes(text)
    assert exp.last_source == "grok" and "volume_above_avg" in _on(out) and DEFAULT_ENTRY_NOTE not in notes
    assert RSI_NOTE in notes


# ------------------------------------------------------------------ 2. RSI-exit default note
def test_rsi_exit_default_note_when_not_mentioned():
    s, notes = keyword_parse_with_notes("buy when price is above EMA 20, take profit 8%, stop loss 4%")
    assert s.exit.rsi_above.enabled and s.exit.rsi_above.threshold == 70  # default unchanged
    assert notes == [RSI_NOTE]
    assert RSI_NOTE == ("RSI exit at 70 is on by default (sells when RSI goes above 70); turn it off in the form if you "
                        "only want take-profit and stop-loss.")


@pytest.mark.parametrize("text", [
    "buy when RSI goes below 35 and price goes above SMA 50, sell when RSI goes above 70",
    "buy when price is above EMA 20, RSI exit at 75",
    "buy when price is above EMA 20, no RSI exit",
])
def test_no_rsi_exit_note_when_user_mentions_it(text):
    _, notes = keyword_parse_with_notes(text)
    assert not any(n.startswith("RSI exit at") for n in notes)


def test_default_entry_text_gets_both_notes():
    _, notes = keyword_parse_with_notes("take profit 10% and stop loss 5%")
    assert notes == [DEFAULT_ENTRY_NOTE, RSI_NOTE]


# ------------------------------------------------------------------ 3. template wording
def _sell(mode=TradingMode.SIM, agent=AgentMode.MANUAL, path=ExecutionPath.SWAP):
    sig = Signal(symbol="NVDAB", action=Action.SELL, source=agent, strategy="MANUAL:NVDAB rule set", preset=None,
                 regime=Regime.SIDEWAYS, reason="RSI_EXIT: RSI(14)=74.1 > 70", conditions_met=["RSI_EXIT"],
                 price=244.6608, exit_reason=ExitReason.RSI_EXIT, position_id="ae9e4711aaaa")
    ex = ExecutionResult(client_order_id="c1", symbol="NVDAB", side=OrderSide.SELL, status=OrderStatus.FILLED, path=path,
                         mode=mode, filled_quantity=6.23139, avg_price=244.6608, notional=1524.58, fee=1.52)
    return Decision(mode=mode, agent_mode=agent, symbol="NVDAB", regime=sig.regime, strategy=sig.strategy, signal=sig,
                    action=Action.SELL, execution=ex, reason=sig.reason)


def test_sim_manual_sell_plain_wording():
    txt = templates.explain_decision(_sell())
    assert txt.startswith("Sell NVDAB (manual mode, simulation).")
    assert "Reason: RSI exit, RSI(14)=74.1 is above 70." in txt and "Rsi exit" not in txt
    assert "filled via a swap" in txt and not CODE.search(txt.replace("NVDAB", "")), txt


def test_paper_auto_rfq_wording():
    txt = templates.explain_decision(_sell(TradingMode.PAPER, AgentMode.AUTO, ExecutionPath.RFQ))
    assert txt.startswith("Sell NVDAB (auto mode, paper trading).") and "filled via an RFQ quote" in txt


def test_live_keeps_live_wording():
    txt = templates.explain_decision(_sell(TradingMode.LIVE))
    assert txt.startswith("LIVE, real funds") and "via a swap" in txt and "simulation" not in txt


def test_exit_reason_comparators():
    assert templates.humanize_reason("STOP_LOSS: pnl -4.2% <= -4") == "Stop-loss, pnl -4.2% is at or below -4"
    assert templates.humanize_reason("TRAILING_STOP: close 99.1 < trail 99.5") == "Trailing stop, close 99.1 is below trail 99.5"


def test_pending_buy_reason_softened():
    raw = ("BUY not executed: awaiting your approval of the pending BUY (30600a72a2924faf80b897e67841292f); "
           "no new order until you approve or reject it")
    t = templates.humanize_reason(raw)
    assert t == ("Buy not executed: awaiting your approval of the pending buy; no new order until you approve or "
                 "reject it")
    uuid = "BUY not executed: awaiting your approval of the pending BUY (30600a72-a292-4faf-80b8-97e67841292f)"
    assert "30600a72" not in templates.humanize_reason(uuid)
    d = _sell()
    d = d.model_copy(update={"action": Action.STAY_OUT, "execution": None, "reason": raw})
    txt = templates.explain_decision(d)
    assert "pending buy;" in txt and not CODE.search(txt.replace("NVDAB", "")), txt


def test_numbers_in_parentheses_untouched():
    assert "(1/1)" in templates.humanize_reason("Max open positions reached (1/1); no exit triggered")
    assert "EMA(20)" in templates.humanize_reason("Entry conditions met (AND): close 240.5 > EMA(20) 238.4")

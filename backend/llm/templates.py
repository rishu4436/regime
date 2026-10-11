"""Deterministic, offline text templates. Same input -> same output. Used whenever Grok is unavailable."""
from __future__ import annotations

import re
from typing import Optional, Sequence

from backend.contracts import (
    Action, Alert, AlertLevel, Decision, ExitReason, OrderStatus, PerformanceSummary, Position, StatusSnapshot,
    TradingMode,
)

from . import live as live_facts

EXIT_REASON_TEXT: dict[ExitReason, str] = {
    ExitReason.TRAILING_STOP: "the trailing stop was hit",
    ExitReason.TAKE_PROFIT: "the take-profit target was reached",
    ExitReason.STOP_LOSS: "the stop-loss was hit",
    ExitReason.RSI_EXIT: "RSI rose above the exit level",
    ExitReason.MACD_EXIT: "MACD crossed below its signal line",
    ExitReason.END: "the backtest ended",
    ExitReason.MAX_LOSS: "the max-loss-per-trade limit was reached",
    ExitReason.MANUAL: "you closed it manually",
    ExitReason.EMERGENCY_STOP: "the emergency stop closed it",
}
MAX_ALERTS = 3


def _money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def _price(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.4f}".rstrip("0").rstrip(".")


def _human(name: str) -> str:
    return name.replace("_", " ").lower()


def _sentence(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?" else s + "."


def short_id(x: str) -> str:
    return x[:8]


def _exit_sentence(d: Decision) -> Optional[str]:
    """'Sold position ab12cd34 of NVDAon at 150.5 because the trailing stop was hit.' (None if not an exit)."""
    sig = d.signal
    if sig is None or not (sig.exit_reason or sig.position_id or sig.action == Action.SELL):
        return None
    ex = d.execution
    filled = ex is not None and ex.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and ex.filled_quantity > 0
    fill = ex.avg_price if filled and ex.avg_price else sig.price
    pos = f"position {short_id(sig.position_id)} of {d.symbol}" if sig.position_id else f"the {d.symbol} position"
    why = f" because {EXIT_REASON_TEXT[sig.exit_reason]}" if sig.exit_reason else ""
    at = f" at {_price(fill)}" if fill is not None else ""
    if filled:
        txt = f"Sold {pos}{at}{why}"
        if sig.price is not None and fill is not None and abs(fill - sig.price) > 1e-9:
            txt += f" (intended fill {_price(sig.price)})"
        if ex.status == OrderStatus.PARTIALLY_FILLED:
            txt += ", partially filled"
        return txt + "."
    if d.action == Action.SELL:
        return f"Selling {pos}{at.replace(' at ', ' at an intended fill of ')}{why}."
    return f"Wanted to sell {pos}{at}{why}, but it was not executed."


def explain_decision(d: Decision) -> str:
    if live_facts.is_live(d):
        return explain_live_decision(d)
    verb = {Action.BUY: "Buy", Action.SELL: "Sell", Action.STAY_OUT: "Stay out of"}[d.action]
    parts = [f"{verb} {d.symbol} ({d.agent_mode.value} mode, {d.mode.value})."]
    ctx = f"Strategy: {d.strategy}"
    if d.regime is not None:
        ctx += f", market regime: {_human(d.regime.value)}"
    parts.append(ctx + ".")
    exit_txt = _exit_sentence(d)
    if exit_txt:
        parts.append(exit_txt)
    if d.reason:
        parts.append("Reason: " + _sentence(d.reason))
    sig = d.signal
    if sig is not None:
        if sig.conditions_met:
            parts.append("Conditions met: " + ", ".join(_human(c) for c in sig.conditions_met) + ".")
        if sig.conditions_failed:
            parts.append("Conditions not met: " + ", ".join(_human(c) for c in sig.conditions_failed) + ".")
        if sig.price is not None and not exit_txt:
            parts.append(f"Price at decision: {_price(sig.price)}.")
    v = d.risk_verdict
    if v is not None:
        if v.approved:
            txt = f"Risk check passed (size {v.adjusted_size_pct:.1f}%, {_money(v.adjusted_notional)})"
            if v.requires_confirmation:
                txt += "; waiting for your confirmation before executing"
            parts.append(txt + ".")
        else:
            reasons = "; ".join(v.reasons) or ", ".join(c.name for c in v.checks if not c.passed) or "no reason given"
            parts.append(f"Risk layer blocked the trade: {_sentence(reasons)}")
    ex = d.execution
    if ex is not None:
        txt = f"Execution: {ex.status.value} via {ex.path.value}"
        if ex.filled_quantity:
            txt += f", {ex.filled_quantity:g} {ex.symbol} @ {_price(ex.avg_price)} ({_money(ex.notional)}, fee {_money(ex.fee)})"
        if ex.error:
            txt += f" - {ex.error}"
        parts.append(txt + ".")
    elif d.action != Action.STAY_OUT and (v is None or v.approved) and not (v and v.requires_confirmation):
        parts.append("Not executed yet.")
    return " ".join(parts)


# ------------------------------------------------------------------ LIVE (real funds, autonomous 24/7)
_LIVE_DONE = {"BUY": "bought", "SELL": "sold"}
_REDUCED_RE = re.compile(r"reduced from ([\d,.]+) to ([\d,.]+) USDT")


def live_execution_sentence(d: Decision) -> Optional[str]:
    """Real-funds execution sentence: amount, route, fill price, price impact, tx hash + BscScan link."""
    ex = d.execution
    if ex is None:
        return None
    f = live_facts.execution_facts(ex)
    status = (f["status"] or "").upper()
    side = (f["side"] or ("SELL" if d.action == Action.SELL else "BUY")).upper()
    sym = f["symbol"] or d.symbol
    route = f" via {f['route']}" if f["route"] else ""
    if status in ("FILLED", "PARTIALLY_FILLED") and (f["quantity"] or f["amount_usdt"]):
        amt = f"{f['amount_usdt']:,.2f} USDT" if f["amount_usdt"] is not None else "an unreported USDT amount"
        txt = (f"The agent {_LIVE_DONE.get(side, side.lower())} {sym} on its own with real funds on BNB Chain mainnet: "
               f"{amt}{route}")
        if f["fill_price"] is not None:
            txt += f" at a fill price of {_price(f['fill_price'])}"
        if f["quantity"]:
            txt += f" ({f['quantity']:g} {sym}" + (f", fee {_money(f['fee'])})" if f["fee"] else ")")
        if status == "PARTIALLY_FILLED":
            txt += ", partially filled"
        txt += "."
        if f["price_impact_pct"] is not None:
            txt += f" Price impact {f['price_impact_pct']:.2f}%."
    else:
        why = f": {live_facts.plain_reason(f['error'])}" if f["error"] else ""
        txt = f"The live {side} order for {sym}{route} was not filled ({status or 'unknown status'}){why}."
        if status in ("FAILED", "REJECTED", "EXPIRED", "CANCELLED", "CANCELED") or f["error"]:
            txt += " No position change from this order."
    if f["tx_hash"]:
        txt += f" Tx hash {f['tx_hash']}" + (f": {f['explorer_url']}" if f["explorer_url"] else "") + "."
    elif f["explorer_url"]:
        txt += f" BscScan: {f['explorer_url']}."
    return txt


def live_block_reasons(d: Decision) -> list[str]:
    """Plain-language risk block reasons for a LIVE decision (each names the limit hit, numbers kept)."""
    v = d.risk_verdict
    if v is None or v.approved:
        return []
    raw = list(v.reasons) or [f"Blocked ({c.name}): {c.detail}" for c in v.checks if not c.passed]
    return live_facts.plain_reasons(raw) or ["no reason given"]


def live_size_cuts(d: Decision) -> list[str]:
    """'size cut from 30.00 to 25.00 USDT by the per-order cap (set by the agent's risk limits)' for capped approvals."""
    v = d.risk_verdict
    if v is None or not v.approved:
        return []
    out = []
    for c in v.checks:
        m = _REDUCED_RE.search(c.detail or "")
        if m and c.passed:
            limit = (live_facts.REASON_TEXT.get(c.name) or live_facts.plain_reason(f"{c.name} {c.detail}").split(" (")[0])
            out.append(f"size cut from {m.group(1)} to {m.group(2)} USDT by {limit}")
    return out


def explain_live_decision(d: Decision) -> str:
    """LIVE decision: the agent acts on its own with real funds; never approval/confirmation wording."""
    verb = {Action.BUY: "Buy", Action.SELL: "Sell", Action.STAY_OUT: "Stay out of"}[d.action]
    parts = [f"LIVE, real funds, autonomous: {verb} {d.symbol} ({d.agent_mode.value} mode)."]
    ctx = f"Strategy: {d.strategy}"
    if d.regime is not None:
        ctx += f", market regime: {_human(d.regime.value)}"
    parts.append(ctx + ".")
    sig = d.signal
    exit_txt = _exit_sentence(d)
    filled_exit = exit_txt is not None and exit_txt.startswith("Sold")
    if exit_txt and not filled_exit:
        parts.append(exit_txt)
    elif filled_exit and sig is not None and sig.exit_reason:
        parts.append(f"Exit trigger: {EXIT_REASON_TEXT[sig.exit_reason]}.")
    if d.reason:
        parts.append("Reason: " + _sentence(d.reason))
    if sig is not None:
        if sig.conditions_met:
            parts.append("Conditions met: " + ", ".join(_human(c) for c in sig.conditions_met) + ".")
        engine_codes = [c for c in sig.conditions_failed if c in live_facts.REASON_TEXT]
        others = [c for c in sig.conditions_failed if c not in live_facts.REASON_TEXT]
        if engine_codes:
            parts.append("Stayed out because " + "; ".join(live_facts.plain_reason(c) for c in engine_codes) + ".")
        if others:
            parts.append("Conditions not met: " + ", ".join(_human(c) for c in others) + ".")
        if sig.price is not None and not exit_txt and d.execution is None:
            parts.append(f"Price at decision: {_price(sig.price)}.")
    v = d.risk_verdict
    if v is not None:
        if v.approved:
            cuts = live_size_cuts(d)
            parts.append(f"Live risk checks passed ({_money(v.adjusted_notional)})" + ("; " + "; ".join(cuts) if cuts else "") + ".")
        else:
            parts.append("Risk blocked the trade, no real funds were spent: " + _sentence("; ".join(live_block_reasons(d))))
    ex_txt = live_execution_sentence(d)
    if ex_txt:
        parts.append(ex_txt)
    elif d.action != Action.STAY_OUT and (v is None or v.approved):
        parts.append("The agent is placing this order on its own; no fill yet.")
    return " ".join(parts)


def _position_line(p: Position) -> str:
    line = f"{p.symbol} #{short_id(p.id)}: {p.quantity:g} @ {_price(p.avg_entry_price)}"
    if p.current_price is not None:
        line += f" (now {_price(p.current_price)})"
    line += f", unrealized {_money(p.unrealized_pnl)} [{p.strategy}]"
    return line


def control_line(s: StatusSnapshot) -> str:
    c = s.control
    if c.emergency_stopped:
        state = "EMERGENCY STOPPED"
    elif c.paused:
        state = "paused"
    else:
        state = "running"
    txt = f"Agent is {state} in {c.trading_mode.value} mode"
    if c.trading_mode == TradingMode.LIVE:
        txt += ", trading autonomously 24/7 with real funds on BNB Chain mainnet"
    else:
        txt += ", trades need your confirmation" if c.confirmation_mode else ", trades auto-execute"
    if c.reason:
        txt += f" ({c.reason})"
    return txt + "."


def performance_summary_line(perf: PerformanceSummary) -> str:
    txt = (f"Performance: equity {_money(perf.equity)} ({_pct(perf.total_return_pct)} vs start {_money(perf.starting_capital)}), "
           f"realized {_money(perf.realized_pnl)}, unrealized {_money(perf.unrealized_pnl)}, max drawdown {perf.max_drawdown_pct:.2f}%, "
           f"win rate {perf.win_rate_pct:.1f}% over {perf.num_trades} closed trade(s) ({perf.wins} won, {perf.losses} lost)")
    if perf.consecutive_losses:
        txt += f", {perf.consecutive_losses} loss(es) in a row"
    return txt + "."


_LEVEL_RANK = {AlertLevel.CRITICAL: 0, AlertLevel.WARNING: 1, AlertLevel.INFO: 2}


def alerts_line(alerts: Optional[Sequence[Alert]]) -> str:
    if not alerts:
        return "No recent alerts."
    # most recent first; ties broken by severity, then input order
    order = sorted(enumerate(alerts), key=lambda ia: (-ia[1].timestamp.timestamp(), _LEVEL_RANK[ia[1].level], -ia[0]))
    shown = [a for _, a in order[:MAX_ALERTS]]
    items = [f"[{a.level.value}] {a.code}" + (f" ({a.symbol})" if a.symbol else "") + f": {a.message.strip()}" for a in shown]
    more = f" (+{len(alerts) - len(shown)} more)" if len(alerts) > len(shown) else ""
    return f"Recent alerts{more}: " + "; ".join(items) + "."


def performance_line(s: StatusSnapshot, perf: Optional[PerformanceSummary] = None) -> str:
    if perf is not None:
        return performance_summary_line(perf)
    p = s.portfolio
    ret = (p.equity - p.starting_capital) / p.starting_capital * 100
    return (f"Equity {_money(p.equity)} ({_pct(ret)} vs start {_money(p.starting_capital)}), cash {_money(p.cash)}, "
            f"realized PnL {_money(p.realized_pnl)}, today {_money(p.daily_pnl)} over {p.trades_today} trade(s).")


def positions_line(s: StatusSnapshot) -> str:
    pos = s.portfolio.positions
    if not pos:
        return "No open positions."
    return f"{len(pos)} open position(s): " + "; ".join(_position_line(p) for p in pos) + "."


def regime_line(s: StatusSnapshot) -> str:
    sym = s.active_symbol or "the active token"
    if s.current_regime is None:
        return f"No market regime detected yet for {sym}."
    return f"Current regime for {sym}: {_human(s.current_regime.value)}."


def strategy_line(s: StatusSnapshot) -> str:
    mode = f" ({s.agent_mode.value} mode)" if s.agent_mode else ""
    if not s.active_strategy:
        return f"No active strategy{mode}."
    return f"Active strategy: {s.active_strategy}{mode}" + (f" on {s.active_symbol}." if s.active_symbol else ".")


def last_decision_line(s: StatusSnapshot) -> str:
    d = s.last_decision or (s.recent_decisions[-1] if s.recent_decisions else None)
    if d is None:
        return "No decisions yet."
    return "Last decision: " + (d.explanation or explain_decision(d))


def summarize_state(s: StatusSnapshot, performance: Optional[PerformanceSummary] = None,
                    alerts: Optional[Sequence[Alert]] = None) -> str:
    parts = [control_line(s), regime_line(s), strategy_line(s), performance_line(s, performance), positions_line(s), last_decision_line(s)]
    if alerts is not None:
        parts.append(alerts_line(alerts))
    return " ".join(parts)


_TRADE_REQUEST = re.compile(r"\b(buy|sell|place|execute|open|close|short)\b.*\b(order|trade|position|now|for me|it)\b|^\s*(buy|sell)\b", re.I)

_INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(alert|warning|problem|issue|wrong|error|fail)", re.I), "alerts"),
    (re.compile(r"\b(why|reason|explain|last decision|last trade|decid)", re.I), "last"),
    (re.compile(r"\b(regime|market condition|trend|volatil|sideways|uptrend|downtrend)", re.I), "regime"),
    (re.compile(r"\b(strateg|preset|style|dca|grid|dip|momentum)", re.I), "strategy"),
    (re.compile(r"\b(position|holding|hold|exposure|own)", re.I), "positions"),
    (re.compile(r"\b(pnl|p&l|profit|loss|perform|return|equity|cash|balance|capital|money|doing|drawdown|win rate|winning|losing|streak)", re.I), "performance"),
    (re.compile(r"\b(paused?|stop|running|status|mode|confirm|live|paper|sim)", re.I), "control"),
]


def answer(question: str, s: StatusSnapshot, performance: Optional[PerformanceSummary] = None,
           alerts: Optional[Sequence[Alert]] = None) -> str:
    q = question or ""
    if _TRADE_REQUEST.search(q):
        controls = ("pause / override / emergency stop" if s.control.trading_mode == TradingMode.LIVE
                    else "confirm / pause / override / emergency stop")
        return ("I can only explain what the agent is doing; I never place, change or cancel trades. "
                f"Use the dashboard controls ({controls}) to act. " + control_line(s))
    builders = {"last": last_decision_line, "regime": regime_line, "strategy": strategy_line,
                "positions": positions_line, "performance": lambda st: performance_line(st, performance), "control": control_line,
                "alerts": lambda st: alerts_line(alerts)}
    hits = [name for pat, name in _INTENTS if pat.search(q)]
    if not hits:
        return summarize_state(s, performance, alerts)
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return " ".join(builders[h](s) for h in seen)

"""Deterministic, offline text templates. Same input -> same output. Used whenever Grok is unavailable."""
from __future__ import annotations

import re
from typing import Optional

from backend.contracts import Action, Decision, Position, StatusSnapshot


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


def explain_decision(d: Decision) -> str:
    verb = {Action.BUY: "Buy", Action.SELL: "Sell", Action.STAY_OUT: "Stay out of"}[d.action]
    parts = [f"{verb} {d.symbol} ({d.agent_mode.value} mode, {d.mode.value})."]
    ctx = f"Strategy: {d.strategy}"
    if d.regime is not None:
        ctx += f", market regime: {_human(d.regime.value)}"
    parts.append(ctx + ".")
    if d.reason:
        parts.append("Reason: " + _sentence(d.reason))
    sig = d.signal
    if sig is not None:
        if sig.conditions_met:
            parts.append("Conditions met: " + ", ".join(_human(c) for c in sig.conditions_met) + ".")
        if sig.conditions_failed:
            parts.append("Conditions not met: " + ", ".join(_human(c) for c in sig.conditions_failed) + ".")
        if sig.price is not None:
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


def _position_line(p: Position) -> str:
    line = f"{p.symbol}: {p.quantity:g} @ {_price(p.avg_entry_price)}"
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
    txt += ", trades need your confirmation" if c.confirmation_mode else ", trades auto-execute"
    if c.reason:
        txt += f" ({c.reason})"
    return txt + "."


def performance_line(s: StatusSnapshot) -> str:
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


def summarize_state(s: StatusSnapshot) -> str:
    return " ".join([control_line(s), regime_line(s), strategy_line(s), performance_line(s), positions_line(s), last_decision_line(s)])


_TRADE_REQUEST = re.compile(r"\b(buy|sell|place|execute|open|close|short)\b.*\b(order|trade|position|now|for me|it)\b|^\s*(buy|sell)\b", re.I)

_INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(why|reason|explain|last decision|last trade|decid)", re.I), "last"),
    (re.compile(r"\b(regime|market condition|trend|volatil|sideways|uptrend|downtrend)", re.I), "regime"),
    (re.compile(r"\b(strateg|preset|style|dca|grid|dip|momentum)", re.I), "strategy"),
    (re.compile(r"\b(position|holding|hold|exposure|own)", re.I), "positions"),
    (re.compile(r"\b(pnl|p&l|profit|loss|perform|return|equity|cash|balance|capital|money|doing)", re.I), "performance"),
    (re.compile(r"\b(paused?|stop|running|status|mode|confirm|live|paper|sim)", re.I), "control"),
]


def answer(question: str, s: StatusSnapshot) -> str:
    q = question or ""
    if _TRADE_REQUEST.search(q):
        return ("I can only explain what the agent is doing; I never place, change or cancel trades. "
                "Use the dashboard controls (confirm / pause / override / emergency stop) to act. " + control_line(s))
    builders = {"last": last_decision_line, "regime": regime_line, "strategy": strategy_line,
                "positions": positions_line, "performance": performance_line, "control": control_line}
    hits = [name for pat, name in _INTENTS if pat.search(q)]
    if not hits:
        return summarize_state(s)
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return " ".join(builders[h](s) for h in seen)

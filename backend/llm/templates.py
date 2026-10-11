"""Deterministic, offline text templates. Same input -> same output. Used whenever Grok is unavailable."""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from backend.contracts import (
    Action, Alert, AlertLevel, Decision, ExitReason, OrderStatus, PerformanceSummary, Position, Regime, StatusSnapshot,
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

# ------------------------------------------------------------------ readable labels (no raw codes in user text)
PRESET_LABEL: dict[str, str] = {
    "DCA": "DCA", "SMART_DCA": "Smart DCA", "BUY_THE_DIP": "Buy the Dip", "TREND_FOLLOWING": "Trend following",
    "GRID": "Grid", "NONE": "no preset (staying out)",
}
STYLE_LABEL: dict[str, str] = {"PATIENT": "Patient", "BALANCED": "Balanced", "ACTIVE": "Active"}
EXIT_LABEL: dict[str, str] = {
    "TRAILING_STOP": "trailing stop", "TAKE_PROFIT": "take-profit", "STOP_LOSS": "stop-loss", "RSI_EXIT": "RSI exit",
    "MACD_EXIT": "MACD exit", "END": "end of backtest", "MAX_LOSS": "max loss per trade", "MANUAL": "manual close",
    "EMERGENCY_STOP": "emergency stop",
}
STATUS_TEXT: dict[str, str] = {
    "FILLED": "filled", "PARTIALLY_FILLED": "partially filled", "PENDING_CONFIRMATION": "pending",
    "REJECTED": "rejected", "EXPIRED": "expired", "FAILED": "failed",
}
ALERT_TEXT: dict[str, str] = {
    "EMERGENCY_STOP": "emergency stop", "PAUSED": "paused", "CONSECUTIVE_LOSSES": "losses in a row",
    "DAILY_LOSS": "daily loss limit", "QUOTE_EXPIRED": "quote expired", "EXECUTION_FAILED": "execution failed",
    "RISK_BLOCKED": "risk blocked a trade", "LIVE_MODE": "live mode", "COMPONENT_FALLBACK": "component fallback",
}
# Signal.conditions_met / conditions_failed names (Bot 2 manual + presets) -> plain words
COND_TEXT: dict[str, str] = {
    "rsi_below": "RSI below the entry level", "price_above_sma": "price above its SMA",
    "price_above_ema": "price above its EMA", "macd_bullish_cross": "MACD crossed above its signal line",
    "bollinger_lower": "price at or near the lower Bollinger band", "volume_above_avg": "volume above average",
    "breakout": "price breakout", "dca_schedule": "scheduled DCA bar", "near_sma20": "price close to its 20-bar SMA",
    "drop_from_high": "big enough drop from the 20-bar high", "rsi_recovered": "RSI recovered",
    "close_above_ema": "price above the 20-bar EMA", "ema_above_sma": "20-bar EMA above the 50-bar SMA",
    "macd_above_signal": "MACD above its signal line", "trend_broken": "trend broken",
    "top_cell": "price in the top grid cell", "bottom_cell": "price in the bottom grid cell",
    "in_range": "price inside the grid range", "range": "a usable grid range", "warmup": "indicators ready",
    "market_hours": "market-hours filter", "min_volume": "minimum-volume filter",
}


def _human(name: str) -> str:
    return name.replace("_", " ").lower()


def strategy_label(s: Optional[str]) -> str:
    if not s:
        return PRESET_LABEL["NONE"]
    if s in PRESET_LABEL:
        return PRESET_LABEL[s]
    if s.startswith("MANUAL:"):
        return f'manual rule set "{s[7:]}"'
    return s


def cond_text(c: str) -> str:
    return COND_TEXT.get(c) or EXIT_LABEL.get(c) or _human(c)


def status_text(status: Any) -> str:
    v = str(getattr(status, "value", status) or "")
    return STATUS_TEXT.get(v.upper(), _human(v))


_REGIME_RE = re.compile(r"\b(" + "|".join(r.value for r in Regime) + r")\b")
_PRESET_RE = re.compile(r"\b(SMART_DCA|BUY_THE_DIP|TREND_FOLLOWING|GRID)\b")
_EXIT_RE = re.compile(r"\b(" + "|".join(k for k in EXIT_LABEL if "_" in k) + r")\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_UPPER_SNAKE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_AWAIT_PREFIX_RE = re.compile(r"^\s*awaiting user confirmation[^:]*:\s*", re.I)
_NOT_EXECUTED_RE = re.compile(r"^\s*(?:BUY|SELL|STAY_OUT) not executed:|^\s*approved by user but not executed:", re.I)
_LIVE_SIZE_RE = re.compile(r"\s*\[live-safe size ([^\]]*?)=\s*([\d.,]+) USDT\]")
# Small buys raised to the token / risk minimum (Bot 2 'bumped 2.50 -> ondo min order 20 USDT', Bot 3 'raised from
# 6.25 to 10.00 USDT (min order)' / 'increased to ...'): read as 'size raised to the token minimum (X USDT)'.
_RAISED_RE = re.compile(r"(?:\b(?:the\s+)?(?:order|size|buy|notional|amount)\s+(?:was\s+)?)?\b(?:raised|increased|bumped)(?:\s+(?:up\s+)?from)?\s*(?:[\d.,]+\s*(?:USDT)?\s*)?(?:to|->)\s*"
                        r"(?:the\s+)?(?:[a-z]+\s+)?(?:token\s+)?(?:min(?:imum)?(?:[ _]order(?:[ _]usdt)?)?\s*:?\s*)?([\d.,]+)\s*USDT"
                        r"(?:\s*\((?:[^()]*min[^()]*)\))?", re.I)


def raised_text(amount: str) -> str:
    return f"size raised to the token minimum ({amount} USDT)"


def _live_size_note(m: "re.Match[str]") -> str:
    steps, amount = m.group(1), m.group(2)
    r = _RAISED_RE.search(steps)
    if r or re.search(r"\b(raised|increased|bumped)\b", steps, re.I):
        return "; " + raised_text(amount)
    return f"; live-safe size {amount} USDT"
_BLOCKED_SEG_RE = re.compile(r"Blocked \([a-z0-9_]+\):[^;]*")
# Engine price-impact detail in free text (alerts, reasons): 'quote price impact 1.5% > 1% (direction not reported, ...)'
_UNSIGNED_SEG_RE = re.compile(r"(?:quote )?price impact [^;()]*\((?:direction not reported|counted as adverse)[^)]*\)", re.I)


# 'RSI_EXIT: RSI(14)=74.1 > 70' -> 'RSI exit, RSI(14)=74.1 is above 70' (label keeps its own capitals, comparators in words)
_EXIT_PREFIX_RE = re.compile(r"^(" + "|".join(k for k in EXIT_LABEL) + r"):\s*([^;]*)")
_CMP_WORDS = ((">=", "is at or above"), ("<=", "is at or below"), (">", "is above"), ("<", "is below"))


def _cmp_words(t: str) -> str:
    for op, words in _CMP_WORDS:
        o = re.escape(op)
        t = re.sub(r"\s+" + o + r"(?!=)\s+|\s*" + o + r"(?!=)\s*(?=[-+]?\d)", f" {words} ", t)
    return t


def _exit_prefix(m: "re.Match[str]") -> str:
    label = EXIT_LABEL[m.group(1)]
    rest = _cmp_words(m.group(2).strip())
    return label[:1].upper() + label[1:] + (f", {rest}" if rest else "")


# Loop reason 'BUY not executed: awaiting your approval of the pending BUY (30600a72...); ...' is shown to the user:
# lowercase the actions and drop the internal id in parentheses.
_PAREN_ID_RE = re.compile(r"\s*\((?:id[:\s]*)?(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,})\)", re.I)


def _pending_soften(t: str) -> str:
    t = re.sub(r"^\s*(BUY|SELL|STAY_OUT) not executed:", lambda m: _human(m.group(1)).capitalize() + " not executed:", t)
    t = re.sub(r"\b(pending|the|a|your) (BUY|SELL)\b", lambda m: f"{m.group(1)} {m.group(2).lower()}", t)
    if re.search(r"pending|approv|confirm", t, re.I):
        t = _PAREN_ID_RE.sub("", t)
    return t


MODE_TEXT: dict[str, str] = {"SIM": "simulation", "PAPER": "paper trading", "LIVE": "live"}
ROUTE_TEXT: dict[str, str] = {"SWAP": "a swap", "RFQ": "an RFQ quote"}


def mode_text(d: Decision) -> str:
    """'manual mode, simulation' / 'auto mode, paper trading' (LIVE decisions have their own wording)."""
    tm = str(getattr(d.mode, "value", d.mode) or "")
    return f"{str(getattr(d.agent_mode, 'value', d.agent_mode)).lower()} mode, {MODE_TEXT.get(tm.upper(), tm.lower())}"


def route_text(route: Any) -> str:
    """SWAP / RFQ -> 'a swap' / 'an RFQ quote' (unknown routes as given)."""
    v = str(getattr(route, "value", route) or "")
    return ROUTE_TEXT.get(v.upper(), v)


def humanize_reason(text: Optional[str], keywords: bool = False) -> str:
    """Engine / risk reason string -> plain words: regime + style -> preset mapping, 'Blocked (check): ...',
    exit codes, condition names, live-safe size notes; env-var names removed. Numbers are kept."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _AWAIT_PREFIX_RE.sub("", t)
    t = _BLOCKED_SEG_RE.sub(lambda m: live_facts.plain_reason(m.group(0), keywords), t)
    t = _UNSIGNED_SEG_RE.sub(lambda m: live_facts.price_impact_text("live_price_impact", m.group(0)) or m.group(0), t)
    t = _LIVE_SIZE_RE.sub(_live_size_note, t)
    t = _RAISED_RE.sub(lambda m: raised_text(m.group(1)), t)
    t = re.sub(r"^Regime (?=[A-Z_]+\b)", "Market regime ", t)
    t = _REGIME_RE.sub(lambda m: _human(m.group(1)), t)
    t = re.sub(r"\b([a-z][a-z ]*?) \+ (PATIENT|BALANCED|ACTIVE) -> ([A-Z_]+)",
               lambda m: f"{m.group(1)} market with the {STYLE_LABEL[m.group(2)]} style selects "
                         f"{PRESET_LABEL.get(m.group(3), m.group(3))}", t)
    t = re.sub(r"\b([a-z][a-z ]*?) \+ (PATIENT|BALANCED|ACTIVE):",
               lambda m: f"{m.group(1)} market with the {STYLE_LABEL[m.group(2)]} style:", t)
    t = _pending_soften(t)
    t = _EXIT_PREFIX_RE.sub(_exit_prefix, t)
    t = _EXIT_RE.sub(lambda m: EXIT_LABEL[m.group(1)], t)
    t = _PRESET_RE.sub(lambda m: PRESET_LABEL[m.group(1)], t)
    t = live_facts.ENV_NAME_RE.sub("a live-trading setting", t)
    t = _SNAKE_RE.sub(lambda m: cond_text(m.group(0)), t)
    t = _UPPER_SNAKE_RE.sub(lambda m: _human(m.group(0)), t)
    return " ".join(t.split())


def _money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def _price(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.4f}".rstrip("0").rstrip(".")


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


# ------------------------------------------------------------------ shared pieces (every mode)
def _engine_codes(d: Decision) -> tuple[list[str], list[str]]:
    """(engine/risk codes with a plain-language reason, other failed condition names)."""
    sig = d.signal
    failed = list(sig.conditions_failed) if sig else []
    return ([c for c in failed if c in live_facts.REASON_TEXT], [c for c in failed if c not in live_facts.REASON_TEXT])


def _reason_line(d: Decision, keywords: bool) -> Optional[str]:
    """'Reason: ...' in plain words; skipped when another sentence already says the same thing."""
    v, sig = d.risk_verdict, d.signal
    r = d.reason or ""
    if v is not None and not v.approved and _NOT_EXECUTED_RE.match(r):
        r = sig.reason if sig is not None and sig.reason != r else ""  # the block itself is the risk sentence
    codes, _ = _engine_codes(d)
    if codes and r.lstrip().lower().startswith("live safety blocked entry"):
        r = ""  # covered by 'Stayed out because ...'
    r = humanize_reason(r, keywords)
    return "Reason: " + _sentence(r) if r else None


def reason_text(d: Decision, keywords: bool = True) -> str:
    """The decision's own reason in plain words, minus parts another sentence already gives ('' if none)."""
    rl = _reason_line(d, keywords)
    return rl[len("Reason: "):] if rl else ""


_ENGINE_IMPACT_SEG_RE = re.compile(r"(?:quote )?price impact[^;]*", re.I)


def engine_reason_texts(d: Decision, keywords: bool = True) -> list[str]:
    """Plain words for the engine's live STAY_OUT codes. Price-impact codes use the engine's own detail (numbers) so
    the text is identical to risk's for the same situation (see ``dedupe_block_reasons``)."""
    sig = d.signal
    codes, _ = _engine_codes(d)
    out: list[str] = []
    for c in codes:
        t = None
        if c in live_facts.PRICE_IMPACT_CODES:
            m = _ENGINE_IMPACT_SEG_RE.search((sig.reason if sig is not None else "") or "")
            t = live_facts.price_impact_text(c, m.group(0) if m else "")
        t = t or live_facts.plain_reason(c, keywords)
        if t not in out:
            out.append(t)
    return out


def dedupe_block_reasons(d: Decision, reasons: Sequence[str], keywords: bool = True) -> list[str]:
    """Risk block reasons minus those the engine's stay-out already gave (same situation, same words: say it once)."""
    said = set(engine_reason_texts(d, keywords))
    return [r for r in reasons if r not in said]


SAME_REASON = "for the same reason"


def _conditions_parts(d: Decision, keywords: bool) -> list[str]:
    sig = d.signal
    if sig is None:
        return []
    parts = []
    if sig.conditions_met:
        parts.append("Conditions met: " + ", ".join(cond_text(c) for c in sig.conditions_met) + ".")
    codes, others = _engine_codes(d)
    if codes:
        parts.append("Stayed out because " + "; ".join(engine_reason_texts(d, keywords)) + ".")
    if others:
        parts.append("Conditions not met: " + ", ".join(cond_text(c) for c in others) + ".")
    return parts


def awaiting_confirmation(d: Decision) -> bool:
    """True when the trade was proposed and waits for the user's OK (confirmation mode is opt-in): nothing was sent."""
    if d.action == Action.STAY_OUT:
        return False
    if _AWAIT_PREFIX_RE.match(d.reason or ""):
        return True
    v, ex = d.risk_verdict, d.execution
    if v is None or not v.approved or not v.requires_confirmation:
        return False
    if ex is None:
        return True
    st = str(getattr(ex.status, "value", ex.status))
    return st == "PENDING_CONFIRMATION" and not ex.tx_hash and not ex.error and not ex.filled_quantity


# ------------------------------------------------------------------ small wallet / gas hints (LIVE)
# Bot 3's risk.live_limits() keys, pushed in by the app via set_live_limits() (read defensively; may be absent).
_LIVE_LIMITS: dict[str, Any] = {}
GAS_SUGGESTED_BNB = 0.005
GAS_RESERVE_BNB = 0.002
_MIN_ORDER_RE = re.compile(r"min_order_notional|live_below_min_order|below_min(?:imum|_order)?\b|minimum order|"
                           r"\b40375\b|too small|below (?:the )?(?:token )?min", re.I)
_GAS_RE = re.compile(r"live_gas_reserve|insufficient_gas|not enough gas|\bgas reserve\b", re.I)
_MIN_WALLET_RE = re.compile(r"min(?:imum)?[ _-]?wallet(?:[ _-]?usdt)?\D{0,12}?(\d[\d,]*(?:\.\d+)?)", re.I)


def set_live_limits(limits: Optional[Mapping[str, Any]]) -> None:
    """Latest risk.live_limits() dict (e.g. min_wallet_usdt, bnb_gas_reserve). None / non-mapping clears it."""
    global _LIVE_LIMITS
    _LIVE_LIMITS = dict(limits) if isinstance(limits, Mapping) else {}
    live_facts.set_order_cap(_LIVE_LIMITS.get("per_order_cap_usdt"))


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "")) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None
    return f if f is not None and f > 0 and f == f and f != float("inf") else None


def _blocking_text(d: Decision) -> str:
    """Raw text of whatever blocked / failed this decision (risk reasons + failed checks, engine codes, exec error)."""
    out: list[str] = []
    v = d.risk_verdict
    if v is not None and not v.approved:
        out += list(v.reasons) + [f"{c.name} {c.detail}" for c in v.checks if not c.passed]
    if d.signal is not None:
        out += list(d.signal.conditions_failed)
    err = live_facts.get_field(d.execution, "error")
    if err:
        out.append(str(err))
    return " | ".join(out)


def wallet_hints(d: Decision, limits: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Plain-language small-wallet / gas hints for a blocked or failed buy (empty if not relevant)."""
    lim = {**_LIVE_LIMITS, **(dict(limits) if isinstance(limits, Mapping) else {})}
    blob = _blocking_text(d)
    if not blob or d.action == Action.SELL:
        return []
    hints = []
    if _MIN_ORDER_RE.search(blob):
        mw = _num(lim.get("min_wallet_usdt"))
        if mw is None:
            m = _MIN_WALLET_RE.search(blob)
            mw = _num(m.group(1)) if m else None
        if mw is not None:
            hints.append(f"Small wallet: at this wallet size the buy falls under the minimum order, so the wallet likely "
                         f"needs about {mw:,.0f} USDT or more to trade.")
    if _GAS_RE.search(blob):
        reserve = _num(lim.get("bnb_gas_reserve")) or _num(lim.get("min_gas_reserve")) or GAS_RESERVE_BNB
        hints.append(f"Keep about {max(GAS_SUGGESTED_BNB, reserve):g} BNB in the wallet for gas (the hard reserve is "
                     f"{reserve:g} BNB).")
    return hints


def confirmation_on(d: Decision) -> bool:
    """Confirmation mode genuinely applies to this decision (opt-in), so approval wording is honest."""
    return awaiting_confirmation(d) or bool(d.risk_verdict is not None and d.risk_verdict.approved
                                            and d.risk_verdict.requires_confirmation)


def explain_decision(d: Decision) -> str:
    if live_facts.is_live(d):
        return explain_live_decision(d)
    verb = {Action.BUY: "Buy", Action.SELL: "Sell", Action.STAY_OUT: "Stay out of"}[d.action]
    parts = [f"{verb} {d.symbol} ({mode_text(d)})."]
    ctx = f"Strategy: {strategy_label(d.strategy)}"
    if d.regime is not None:
        ctx += f", market regime: {_human(d.regime.value)}"
    parts.append(ctx + ".")
    exit_txt = _exit_sentence(d)
    if exit_txt:
        parts.append(exit_txt)
    rl = _reason_line(d, keywords=False)
    if rl:
        parts.append(rl)
    parts.extend(_conditions_parts(d, keywords=False))
    sig = d.signal
    if sig is not None and sig.price is not None and not exit_txt:
        parts.append(f"Price at decision: {_price(sig.price)}.")
    v = d.risk_verdict
    ex = d.execution
    waiting = awaiting_confirmation(d)
    if v is not None:
        if v.approved:
            txt = f"Risk check passed (size {v.adjusted_size_pct:.1f}%, {_money(v.adjusted_notional)})"
            cuts = live_size_cuts(d)
            if cuts:
                txt += "; " + "; ".join(cuts)
            if waiting:
                txt += "; waiting for your confirmation before executing"
            parts.append(txt + ".")
        else:
            plain = live_facts.plain_reasons(v.reasons, keywords=False)
            reasons = "; ".join(dedupe_block_reasons(d, plain, keywords=False)) or ", ".join(
                cond_text(c.name) for c in v.checks if not c.passed and not plain)
            if plain and not reasons:
                parts.append(f"Risk layer blocked the trade too, {SAME_REASON}.")
            else:
                parts.append(f"Risk layer blocked the trade: {_sentence(reasons or 'no reason given')}")
    if ex is not None and not waiting:
        txt = f"Execution: {status_text(ex.status)} via {route_text(ex.path)}"
        if ex.filled_quantity:
            txt += f", {ex.filled_quantity:g} {ex.symbol} @ {_price(ex.avg_price)} ({_money(ex.notional)}, fee {_money(ex.fee)})"
        if ex.error:
            txt += f" - {humanize_reason(ex.error)}"
        parts.append(txt + ".")
    elif waiting and v is None:
        parts.append("Waiting for your confirmation before executing.")
    elif ex is None and d.action != Action.STAY_OUT and (v is None or v.approved):
        parts.append("Not executed yet.")
    return " ".join(parts)


# ------------------------------------------------------------------ LIVE (real funds; autonomous unless confirmation is on)
_LIVE_DONE = {"BUY": "bought", "SELL": "sold"}
_REDUCED_RE = re.compile(r"reduced from ([\d,.]+) to ([\d,.]+) USDT")
AWAITING_SENTENCE = ("Regime proposed this trade and it is waiting for your OK in the dashboard; nothing was sent and "
                     "no real funds moved.")


def live_execution_sentence(d: Decision, own: bool = True) -> Optional[str]:
    """Real-funds execution sentence: amount, route, fill price, price impact, tx hash + BscScan link.
    ``own=False`` (confirmation mode: the user OK'd it) drops the 'on its own' wording."""
    ex = d.execution
    if ex is None:
        return None
    f = live_facts.execution_facts(ex)
    status = (f["status"] or "").upper()
    side = (f["side"] or ("SELL" if d.action == Action.SELL else "BUY")).upper()
    sym = f["symbol"] or d.symbol
    route = f" via {route_text(f['route'])}" if f["route"] else ""
    how = " on its own" if own else " after your OK"
    if status in ("FILLED", "PARTIALLY_FILLED") and (f["quantity"] or f["amount_usdt"]):
        amt = f"{f['amount_usdt']:,.2f} USDT" if f["amount_usdt"] is not None else "an unreported USDT amount"
        txt = (f"Regime {_LIVE_DONE.get(side, side.lower())} {sym}{how} with real funds on BNB Chain mainnet: "
               f"{amt}{route}")
        if f["fill_price"] is not None:
            txt += f" at a fill price of {_price(f['fill_price'])}"
        if f["quantity"]:
            txt += f" ({f['quantity']:g} {sym}" + (f", fee {_money(f['fee'])})" if f["fee"] else ")")
        if status == "PARTIALLY_FILLED":
            txt += ", partially filled"
        txt += "."
        txt += (f" Price impact {f['price_impact_pct']:.2f}%." if f["price_impact_pct"] is not None
                else " Price impact unknown.")
    elif status == "PENDING_CONFIRMATION":
        amt = f" for {f['amount_usdt']:,.2f} USDT" if f["amount_usdt"] is not None else ""
        txt = (f"Regime submitted a {side} of {sym}{amt}{route}{how} with real funds; the transaction is "
               f"waiting to settle on BNB Chain (not filled yet).")
    else:
        why = f": {live_facts.plain_reason(f['error'])}" if f["error"] else ""
        txt = f"The live {side} order for {sym}{route} was not filled ({status_text(status) if status else 'unknown status'}){why}."
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
    """'size cut from 30.00 to 25.00 USDT by the per-order cap (set by Regime's risk limits)' for capped approvals,
    'size raised to the token minimum (10.00 USDT)' when risk raised a small buy to the minimum order."""
    v = d.risk_verdict
    if v is None or not v.approved:
        return []
    out = []
    for c in v.checks:
        r = _RAISED_RE.search(c.detail or "")
        if r and c.passed:
            t = raised_text(r.group(1))
            if t not in out:
                out.append(t)
            continue
        m = _REDUCED_RE.search(c.detail or "")
        if m and c.passed:
            limit = (live_facts.REASON_TEXT.get(c.name) or live_facts.plain_reason(f"{c.name} {c.detail}").split(" (")[0])
            out.append(f"size cut from {m.group(1)} to {m.group(2)} USDT by {limit}")
    return out


def explain_live_decision(d: Decision) -> str:
    """LIVE decision. Autonomous (AUTO_EXECUTE): never approval wording. Confirmation mode (opt-in): honest wording,
    and a proposed-but-unconfirmed trade is never described as submitted or as moving real funds."""
    verb = {Action.BUY: "Buy", Action.SELL: "Sell", Action.STAY_OUT: "Stay out of"}[d.action]
    waiting = awaiting_confirmation(d)
    conf = confirmation_on(d)
    head = "LIVE, real funds, confirmation mode" if conf else "LIVE, real funds, autonomous"
    parts = [f"{head}: {verb} {d.symbol} ({d.agent_mode.value} mode)" + (", proposed." if waiting else ".")]
    ctx = f"Strategy: {strategy_label(d.strategy)}"
    if d.regime is not None:
        ctx += f", market regime: {_human(d.regime.value)}"
    parts.append(ctx + ".")
    sig = d.signal
    exit_txt = _exit_sentence(d)
    filled_exit = exit_txt is not None and exit_txt.startswith("Sold")
    if exit_txt and not filled_exit and not waiting:
        parts.append(exit_txt)
    elif (filled_exit or waiting) and sig is not None and sig.exit_reason:
        parts.append(f"Exit trigger: {EXIT_REASON_TEXT[sig.exit_reason]}.")
    rl = _reason_line(d, keywords=True)
    if rl:
        parts.append(rl)
    parts.extend(_conditions_parts(d, keywords=True))
    if sig is not None and sig.price is not None and not exit_txt and (d.execution is None or waiting):
        parts.append(f"Price at decision: {_price(sig.price)}.")
    v = d.risk_verdict
    if v is not None:
        if v.approved:
            cuts = live_size_cuts(d)
            parts.append(f"Live risk checks passed ({_money(v.adjusted_notional)})" + ("; " + "; ".join(cuts) if cuts else "") + ".")
        else:
            blocks = live_block_reasons(d)
            rest = dedupe_block_reasons(d, blocks)
            if rest:
                parts.append("Risk blocked the trade, no real funds were spent: " + _sentence("; ".join(rest)))
            else:
                parts.append(f"Risk blocked the trade too, {SAME_REASON}; no real funds were spent.")
    if waiting:
        parts.append(AWAITING_SENTENCE)
        return " ".join(parts)
    ex_txt = live_execution_sentence(d, own=not conf)
    if ex_txt:
        parts.append(ex_txt)
    elif d.action != Action.STAY_OUT and (v is None or v.approved):
        parts.append("Regime is placing this order on its own; no fill yet." if not conf
                     else "The order is being placed after your OK; no fill yet.")
    parts.extend(wallet_hints(d))
    return " ".join(parts)


def _position_line(p: Position) -> str:
    line = f"{p.symbol} #{short_id(p.id)}: {p.quantity:g} @ {_price(p.avg_entry_price)}"
    if p.current_price is not None:
        line += f" (now {_price(p.current_price)})"
    line += f", unrealized {_money(p.unrealized_pnl)} [{strategy_label(p.strategy)}]"
    return line


def control_line(s: StatusSnapshot) -> str:
    c = s.control
    if c.emergency_stopped:
        state = "EMERGENCY STOPPED"
    elif c.paused:
        state = "paused"
    else:
        state = "running"
    if c.trading_mode == TradingMode.LIVE and not c.confirmation_mode and state == "running":
        txt = "Regime is trading autonomously 24/7 with real funds on BNB Chain mainnet (LIVE mode)"
    else:
        txt = f"Regime is {state} in {c.trading_mode.value} mode"
    if c.trading_mode == TradingMode.LIVE:
        if c.confirmation_mode:
            txt += (" with real funds on BNB Chain mainnet; confirmation mode is on, so each trade waits for your OK in "
                    "the dashboard")
        elif state != "running":
            txt += ", real funds on BNB Chain mainnet; when running it trades autonomously 24/7"
    else:
        txt += ", trades need your confirmation" if c.confirmation_mode else ", trades auto-execute"
    if c.reason:
        txt += f" ({humanize_reason(c.reason)})"
    return txt + "."


def performance_summary_line(perf: PerformanceSummary) -> str:
    txt = (f"Performance: equity {_money(perf.equity)} ({_pct(perf.total_return_pct)} vs start {_money(perf.starting_capital)}), "
           f"realized {_money(perf.realized_pnl)}, unrealized {_money(perf.unrealized_pnl)}, max drawdown {perf.max_drawdown_pct:.2f}%, "
           f"win rate {perf.win_rate_pct:.1f}% over {perf.num_trades} closed trade(s) ({perf.wins} won, {perf.losses} lost)")
    if perf.consecutive_losses:
        txt += f", {perf.consecutive_losses} loss(es) in a row"
    return txt + "."


_LEVEL_RANK = {AlertLevel.CRITICAL: 0, AlertLevel.WARNING: 1, AlertLevel.INFO: 2}


_LEVEL_WORD = {AlertLevel.CRITICAL: "Critical: ", AlertLevel.WARNING: "Warning: ", AlertLevel.INFO: ""}


def alerts_line(alerts: Optional[Sequence[Alert]]) -> str:
    if not alerts:
        return "No recent alerts."
    # most recent first; ties broken by severity, then input order
    order = [a for _, a in sorted(enumerate(alerts), key=lambda ia: (-ia[1].timestamp.timestamp(),
                                                                    _LEVEL_RANK[ia[1].level], -ia[0]))]
    # an alert repeated every tick is listed once, at its most recent position, with a count
    groups: dict[tuple, list] = {}
    for a in order:
        key = (a.level, a.code, a.symbol, " ".join(a.message.split()))
        groups.setdefault(key, [a, 0])[1] += 1
    distinct = list(groups.values())
    shown = distinct[:MAX_ALERTS]
    items = [f"{_LEVEL_WORD[a.level]}{ALERT_TEXT.get(a.code, _human(a.code))}" + (f" ({a.symbol})" if a.symbol else "")
             + f": {humanize_reason(a.message.strip())}" + (f" ({n} times)" if n > 1 else "") for a, n in shown]
    more = f" (+{len(distinct) - len(shown)} more)" if len(distinct) > len(shown) else ""
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
    return f"Active strategy: {strategy_label(s.active_strategy)}{mode}" + (f" on {s.active_symbol}." if s.active_symbol else ".")


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
        controls = ("pause / override / emergency stop"
                    if s.control.trading_mode == TradingMode.LIVE and not s.control.confirmation_mode
                    else "confirm / pause / override / emergency stop")
        return ("I can only explain what Regime is doing; I never place, change or cancel trades. "
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

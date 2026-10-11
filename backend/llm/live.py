"""LIVE-mode (real funds, BNB Chain mainnet, fully autonomous 24/7) explanation helpers. Pure, offline, deterministic.

The explainer never trades. These helpers only read facts that the trading loop already produced:

* ``is_live(decision)``: decision / execution in TradingMode.LIVE (or an execution carrying ``real_funds=True``).
* ``execution_facts(execution)``: amount (USDT), route (SWAP/RFQ), fill price, tx hash, BscScan link, price impact.
  Read defensively (attribute, pydantic ``model_extra`` or dict key) because contracts may not carry every field yet.
* ``plain_reason(text)``: turns risk / engine / exchange reason codes and strings into plain language that names the
  limit that was hit. Unknown reasons pass through cleaned up.

Reason sources (matched by name):
  Bot 3 risk (bot3/live 53907b5): ``RiskVerdict.reasons`` = "Blocked (<check>): <detail>" and ``RiskCheck.name``.
  Bot 2 engine (bot2/live fda04a4+): ``Signal.conditions_failed`` codes ``live_*`` and ``insufficient_data``.
  Bot 4 exchange (PR #5 9a958cc): ``ExecutionResult.error`` strings with Binance Web3 error codes (e.g. "40375 ...").
"""
from __future__ import annotations

import re
from typing import Any, Optional

BSCSCAN_TX = "https://bscscan.com/tx/"
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# ----------------------------------------------------------------------------------------------- field access

def get_field(obj: Any, *names: str) -> Any:
    """First non-None value of ``names`` on obj (attribute, pydantic extra, or dict key). Never raises."""
    if obj is None:
        return None
    for n in names:
        try:
            if isinstance(obj, dict):
                v = obj.get(n)
            else:
                v = getattr(obj, n, None)
                if v is None:
                    extra = getattr(obj, "model_extra", None) or {}
                    v = extra.get(n) if isinstance(extra, dict) else None
        except Exception:  # noqa: BLE001 - defensive read
            v = None
        if v is not None:
            return v
    return None


def _enum_val(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(getattr(v, "value", v))


def _float(v: Any) -> Optional[float]:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "live")
    return bool(v)


def is_live(decision: Any) -> bool:
    if _enum_val(get_field(decision, "mode")) == "LIVE":
        return True
    ex = get_field(decision, "execution")
    return _enum_val(get_field(ex, "mode")) == "LIVE" or _truthy(get_field(ex, "real_funds", "realFunds"))


def explorer_url(execution: Any) -> Optional[str]:
    """BscScan link: the supplied ``explorer_url`` if it is a bscscan.com/tx link, else built from a real tx hash."""
    url = get_field(execution, "explorer_url", "explorerUrl", "bscscan_url")
    if isinstance(url, str) and url.startswith(BSCSCAN_TX):
        return url.strip()
    tx = get_field(execution, "tx_hash", "txHash")
    if isinstance(tx, str) and _TX_RE.match(tx.strip()):
        return BSCSCAN_TX + tx.strip()
    return None


def execution_facts(execution: Any) -> dict[str, Any]:
    """Normalized execution facts for templates and the Grok prompt (keys always present, values may be None)."""
    ex = execution
    route = _enum_val(get_field(ex, "route", "path"))
    amount = _float(get_field(ex, "amount_usdt", "notional_usdt", "amount", "notional"))
    if amount is not None and amount <= 0:
        amount = None
    fill = _float(get_field(ex, "fill_price", "avg_price"))
    impact = _float(get_field(ex, "price_impact_pct", "priceImpactPercent", "price_impact"))
    if impact is None:
        impact = _float(get_field(get_field(ex, "quote"), "price_impact_pct", "priceImpactPercent", "price_impact"))
    status = _enum_val(get_field(ex, "status"))
    return {
        "real_funds": _truthy(get_field(ex, "real_funds", "realFunds")) or _enum_val(get_field(ex, "mode")) == "LIVE",
        "status": status,
        "side": _enum_val(get_field(ex, "side")),
        "symbol": get_field(ex, "symbol"),
        "route": route.upper() if route else None,
        "amount_usdt": amount,
        "quantity": _float(get_field(ex, "filled_quantity", "quantity")),
        "fill_price": fill,
        "fee": _float(get_field(ex, "fee")),
        "tx_hash": get_field(ex, "tx_hash", "txHash"),
        "explorer_url": explorer_url(ex),
        "price_impact_pct": impact,
        "error": get_field(ex, "error"),
    }


# ----------------------------------------------------------------------------------------------- reasons
WALLET = "set by the Agentic Wallet"
AGENT = "set by Regime's risk limits"

# ----------------------------------------------------------------------------------------------- price impact
# Bot 3 risk (15beac5) 'live_slippage' is directional: a quote WORSE than the reference price by more than 1% is blocked
# (adverse slippage); a BETTER quote is fine up to a 3% sanity cap, beyond that it is blocked as 'likely a bad quote'
# (sells mirrored). Binance's own priceImpactPercent has no sign, so risk and Bot 2's engine (c89f7e0,
# 'live_price_impact') count its size as adverse: "... (direction not reported, counted as adverse)".
PRICE_IMPACT_CODES = frozenset({"live_slippage", "live_price_impact", "slippage", "price_impact"})
UNSIGNED_IMPACT_TEXT = "Binance didn't say which way the price impact goes, so Regime treated it as a cost to be safe"
_N = r"(\d+(?:\.\d+)?)"
_BAD_QUOTE_RE = re.compile(r"likely a bad quote|sanity (?:limit|cap)", re.I)
_UNSIGNED_RE = re.compile(r"direction not reported|counted as adverse", re.I)
_ADVERSE_RE = re.compile(_N + r"\s*%\s*worse than the (?:reference|market) price", re.I)
_FAVORABLE_RE = re.compile(_N + r"\s*%\s*better than the (?:reference|market) price", re.I)
_SANITY_RE = re.compile(_N + r"\s*%\s*sanity", re.I)
_IMPACT_RE = re.compile(r"price impact\s*(?:of\s*)?-?" + _N + r"\s*%", re.I)
_MAX_RE = re.compile(r"(?:\bmax(?:imum)?|>)\s*" + _N + r"\s*%", re.I)
_TOL_RE = re.compile(r"slippage tolerance\s*" + _N + r"\s*%\s*\(max\s*" + _N + r"\s*%\)", re.I)
_PRICES_RE = re.compile(r"quote\s+" + _N + r"\s+vs\s+reference\s+" + _N, re.I)


def pct_text(x: Optional[float]) -> str:
    """1.5 -> '1.5', 1.0 -> '1', 0.123 -> '0.12' (same rendering for engine '1.5%' and risk '1.50%')."""
    return "?" if x is None else (f"{x:.2f}".rstrip("0").rstrip(".") or "0")


def _num_after(pat: "re.Pattern[str]", text: str, group: int = 1) -> Optional[float]:
    m = pat.search(text or "")
    return _float(m.group(group)) if m else None


def price_impact_text(code: str, detail: Any) -> Optional[str]:
    """Plain words for a price-impact / slippage block (risk 'live_slippage', engine 'live_price_impact').

    The same situation gives the same text from risk and engine (numbers rendered by ``pct_text``), so an explanation
    can drop the duplicate. Returns None for detail it does not recognise (older formats keep the generic wording)."""
    d = _clean(detail or "")
    parts: list[str] = []
    tol_text: Optional[str] = None
    tm = _TOL_RE.search(d)
    if tm:
        tol, tol_cap = _float(tm.group(1)), _float(tm.group(2))
        if tol is not None and tol_cap is not None and tol > tol_cap + 1e-9:
            tol_text = (f"the allowed slippage on the swap ({pct_text(tol)}%) is above the {pct_text(tol_cap)}% limit")
        d_main = d[:tm.start()] + d[tm.end():]
    else:
        d_main = d
    if _BAD_QUOTE_RE.search(d_main) or (_FAVORABLE_RE.search(d_main) and "bad quote" in d_main.lower()):
        fav, cap = _num_after(_FAVORABLE_RE, d_main), _num_after(_SANITY_RE, d_main)
        how = (f"{pct_text(fav)}% better, more than {pct_text(cap)}%" if fav is not None and cap is not None
               else f"more than {pct_text(cap)}%" if cap is not None else "far more than normal")
        parts.append(f"the quote was suspiciously better than the market price ({how}), so Regime skipped it as a "
                     f"likely bad quote")
    elif _UNSIGNED_RE.search(d_main) or (code == "live_price_impact" and _IMPACT_RE.search(d_main)):
        size, cap = _num_after(_IMPACT_RE, d_main), _num_after(_MAX_RE, d_main)
        if size is not None and cap is not None and size <= cap + 1e-9:
            pass  # impact within the limit: only the slippage tolerance (if any) failed
        elif size is not None and cap is not None:
            parts.append(f"{UNSIGNED_IMPACT_TEXT}, and at {pct_text(size)}% it is over the {pct_text(cap)}% limit")
        else:
            parts.append(UNSIGNED_IMPACT_TEXT + ", and it is over the price-impact limit")
    elif _ADVERSE_RE.search(d_main):
        size, cap = _num_after(_ADVERSE_RE, d_main), _num_after(_MAX_RE, d_main)
        if cap is None or size is None or size > cap + 1e-9:
            limit = f"more than the {pct_text(cap)}% slippage limit" if cap is not None else "more than the slippage limit"
            parts.append(f"the quote's price was {pct_text(size)}% worse than the market price, {limit}")
    elif _FAVORABLE_RE.search(d_main):
        pass  # a better price within the sanity cap is never a problem
    elif not tol_text:
        return None
    if tol_text:
        parts.append(tol_text)
    return "; ".join(parts) if parts else None


# code -> plain-language reason. "{d}" is not used; details (numbers) are appended in parentheses by plain_reason.
REASON_TEXT: dict[str, str] = {
    # --- Bot 3 risk check names
    "trading_mode": "live trading is not enabled (the live-trading switches in Regime's settings are off)",
    "emergency_stop": "the emergency stop is active, so no new buys are made",
    "paused": "Regime is paused",
    "max_open_positions": "the maximum number of open positions is reached",
    "cooldown": "the cooldown after the last exit on this token has not finished",
    "daily_loss_limit": "the daily loss limit is reached",
    "max_trades_per_day": "the maximum number of trades per day is reached",
    "position_size": "the order could not be sized",
    "cash_available": "insufficient USDT balance",
    "max_token_exposure": "the max exposure limit for this token is reached",
    "max_total_exposure": "the max total exposure limit is reached",
    "max_loss_per_trade": "the max-loss-per-trade limit",
    "live_daily_loss_cap": ("the daily loss cap was hit: Regime triggered an emergency stop on its own and is "
                            "selling all open positions"),
    "live_stale_data": "the latest price data is stale (too old), so Regime will not trade on it",
    "live_future_data": "the latest price data has a timestamp in the future (clock or feed problem)",
    "live_market_hours": "the token's stock market session is closed",
    "live_market_hours_now": "the token's stock market session is closed right now",
    "live_symbol_allowed": "xStock tokens are excluded in live trading (no usable liquidity on BNB Chain)",
    "live_token_status": "the token is not tradable right now (off-hours, closed, no liquidity or reference price only)",
    "wallet_limits": "the Agentic Wallet's spending limits could not be read, so live buys are held back",
    "wallet_allowed_token": "the token is not on the Agentic Wallet's allowed-token list",
    "live_gas_reserve": "not enough BNB is left as the gas reserve",
    "live_quote": "there is no valid price quote",
    "live_slippage": "the quote's slippage / price impact is over the cap",
    "live_quote_notional": "the quote amount is over the size risk allowed or the per-order cap",
    "live_order_cap": f"the per-order cap ({AGENT})",
    "live_max_exposure": "the max open exposure cap for live trading",
    "live_daily_notional": f"the daily live buying cap ({AGENT})",
    "wallet_per_tx_cap": f"the per-transaction limit ({WALLET})",
    "wallet_daily_limit": f"the daily spending limit ({WALLET})",
    "live_wallet_balance": "insufficient USDT balance in the wallet (after the fee buffer)",
    "live_wallet_quote": "insufficient USDT balance in the wallet to pay the quote plus fees",
    "min_order_notional": "the order is below the minimum order size",
    "open_position": "there is no open position to sell",
    "signal_action": "there was nothing to execute",
    # --- Bot 2 engine codes (Signal.conditions_failed)
    "insufficient_data": "there is not enough price history yet (at least 61 valid bars are needed)",
    "live_no_data": "there are no valid price candles for the token",
    "live_no_volume": "the last price bar had zero on-chain volume (illiquid or closed)",
    "live_thin_liquidity": "on-chain liquidity is too thin right now",
    "live_illiquid_family": "xStock tokens are excluded in live trading (no usable liquidity on BNB Chain)",
    "live_token_paused": "the token is paused or closed for trading right now",
    "live_price_impact": UNSIGNED_IMPACT_TEXT + ", and it is over the price-impact limit",
    "live_below_min_order": ("the token's minimum order (e.g. 20 USD for Ondo tokens) would exceed the per-order cap, "
                             "so Regime stays out"),
    # --- Bot 4 / generic exchange reason codes
    "market_closed": "the token's market is closed",
    "offhours": "the token is in off-hours trading",
    "off_hours": "the token is in off-hours trading",
    "closed": "the token's market is closed",
    "no_liquidity": "the token has no liquidity right now",
    "insufficient_liquidity": "there is not enough liquidity for this order",
    "reference_price_only": "only a reference price is available, not an executable one",
    "below_minimum": "the order is below the token's minimum order size (e.g. 20 USD for Ondo tokens)",
    "below_min_order": "the order is below the token's minimum order size (e.g. 20 USD for Ondo tokens)",
    "quote_expired": "the price quote expired before execution (quotes are valid for about 30 seconds)",
    "insufficient_balance": "insufficient USDT balance",
    "insufficient_gas": "not enough BNB is left as the gas reserve",
    "xstock_excluded": "xStock tokens are excluded in live trading (no usable liquidity on BNB Chain)",
    "live_disabled": "live trading is not enabled",
    "live_not_enabled": "live trading is not enabled",
    "slippage": "the quote's slippage / price impact is over the cap",
    "price_impact": "the quote's price impact is over the cap",
    "pending_exit": "the exit is held because the buy is still settling on-chain; it runs as soon as the fill is confirmed",
    "exit_pending": "the exit is held because the buy is still settling on-chain; it runs as soon as the fill is confirmed",
    "order_in_flight": "an earlier swap is still settling on-chain, so Regime waits for it before placing another",
    "session_expired": "the Agentic Wallet session expired, so trading is paused until Rishu re-scans the QR code",
    "wallet_not_ready": "the Agentic Wallet isn't connected yet, so trading is paused until Rishu scans the QR code",
    "above_maximum": "the order is above the token's maximum order size",
    "xstock_disabled": "xStock tokens are excluded in live trading (no usable liquidity on BNB Chain)",
    "slippage_exceeded": ("the swap was rejected on-chain because the fill would have been worse than the slippage cap "
                          "(no trade happened)"),
    "exit_quota_reserve": ("part of the Agentic Wallet's daily allowance is kept in reserve so Regime can still sell "
                           "all open positions today"),
    "wallet_exit_reserve": ("part of the Agentic Wallet's daily allowance is kept in reserve so Regime can still sell "
                            "all open positions today"),
    "exit_quota_shortfall": ("warning: the Agentic Wallet's remaining daily allowance cannot cover selling all open "
                             "positions today"),
    "wallet_limits_stale": "warning: the Agentic Wallet's limits were last read more than 15 minutes ago",
    "wallet_session_expired": ("the Agentic Wallet session expired, so live trading is paused until Rishu re-scans the "
                               "QR code in the Binance App"),
    "stale_price": "the latest price data is stale (too old), so Regime will not trade on it",
}

# Binance Web3 API error codes (Bot 4 errors.py; trading-api/error-codes)
EXCHANGE_CODE_TEXT: dict[int, str] = {
    40001: "the exchange rejected the request parameters",
    40101: "the Binance API key is invalid",
    40102: "the Binance API signature was rejected",
    40103: "the request timestamp expired",
    40104: "the API key has no permission for this",
    40301: "Binance restricts this region",
    42900: "the exchange rate limit was hit",
    40401: "the price quote expired before execution (quotes are valid for about 30 seconds)",
    40462: "the swap no longer matched its quote",
    40366: "the order is over the Ondo maximum single order size",
    40367: "the token's market is closed (Ondo market state not tradable) - Regime skips this token for a short cooldown",
    40368: "the Ondo stablecoin pair is not valid",
    40369: "the token's market is closed (outside its trading time) - Regime skips this token for a short cooldown",
    40370: "the trading pair is not valid for this token",
    40374: "there is not enough liquidity for this order - Regime skips this token for a short cooldown",
    40375: "the order is below the token's minimum order size (Ondo minimum is 20 USD)",
    42901: "a previous order is still being submitted",
}

# Free-text fallbacks (first match wins), for reasons without a known code.
_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"daily loss", re.I), "live_daily_loss_cap"),
    (re.compile(r"emergency stop", re.I), "emergency_stop"),
    (re.compile(r"per[- ](order|tx|transaction) (cap|limit)", re.I), "live_order_cap"),
    (re.compile(r"slippage|price impact", re.I), "live_slippage"),
    (re.compile(r"\bstale\b|too old", re.I), "live_stale_data"),
    (re.compile(r"quote.{0,20}expired|expired quote", re.I), "quote_expired"),
    (re.compile(r"reference (price )?only", re.I), "reference_price_only"),
    (re.compile(r"off[- ]?hours", re.I), "offhours"),
    (re.compile(r"market (is )?closed|not tradable|invalid trading time", re.I), "market_closed"),
    (re.compile(r"(no|insufficient|thin) liquidity", re.I), "insufficient_liquidity"),
    (re.compile(r"below (the )?(token )?min|minimum order|too small", re.I), "below_min_order"),
    (re.compile(r"session (has )?expired|wallet not connected|baw auth|re-?scan|sign ?in again", re.I), "wallet_session_expired"),
    (re.compile(r"\bgas\b", re.I), "insufficient_gas"),
    (re.compile(r"reserve|quota|allowance", re.I), "exit_quota_reserve"),
    (re.compile(r"insufficient (usdt )?balance|not enough (usdt|cash|balance)", re.I), "insufficient_balance"),
    (re.compile(r"xstock", re.I), "xstock_excluded"),
    (re.compile(r"allowed tokens|allow-?list|whitelist", re.I), "wallet_allowed_token"),
    (re.compile(r"(live|real).{0,30}(disabled|not enabled)|allow_live", re.I), "live_disabled"),
    (re.compile(r"exposure", re.I), "max_total_exposure"),
]

_BLOCKED_RE = re.compile(r"^\s*Blocked \((?P<name>[a-z0-9_]+)\):\s*(?P<detail>.*)$", re.S)
_CODE_RE = re.compile(r"^\s*(?:Binance Web3 API error\s+)?(?P<code>4\d{4})\b[:\s]*(?P<rest>.*)$", re.S)


_SCRUB = [  # LIVE is autonomous: drop/replace approval wording that upstream detail strings may carry
    (re.compile(r"\s*-?\s*awaiting user confirmation", re.I), ""),
    (re.compile(r"\bapproved size\b", re.I), "risk-allowed size"),
    (re.compile(r"\bapproved\b", re.I), "allowed"),
]


def _clean(s: str) -> str:
    out = " ".join(str(s).split()).strip()
    for pat, rep in _SCRUB:
        out = pat.sub(rep, out)
    return out.strip().rstrip(".")


def _token_status_text(detail: str) -> Optional[str]:
    d = detail.lower()
    if "reference" in d:
        return "only a reference price is available for the token, not an executable one"
    if "off" in d and "hour" in d:
        return "the token is in off-hours trading"
    if "no_liquidity" in d or "no liquidity" in d:
        return "the token has no liquidity right now"
    if "paused" in d:
        return "the token is paused for trading right now"
    if "closed" in d:
        return "the token's market is closed"
    return None


def _code_text(code: str, detail: str) -> Optional[str]:
    if code in ("live_token_status", "live_token_paused"):
        return _token_status_text(detail) or REASON_TEXT[code]
    if code == "min_order_notional" and "token minimum" in detail.lower():
        return "the order is below the token's minimum order size (e.g. 20 USD for Ondo tokens)"
    if code == "live_quote" and "expired" in detail.lower():
        return REASON_TEXT["quote_expired"]
    if code == "live_quote" and re.search(r"\bold\b", detail.lower()):
        return "the price quote is too old"
    if code in ("live_quote_notional", "live_order_cap") and "set by wallet" in detail.lower():
        return REASON_TEXT[code].replace(AGENT, WALLET) if AGENT in REASON_TEXT[code] else REASON_TEXT[code] + f" ({WALLET})"
    if code == "live_quote_notional" and "set by risk" in detail.lower():
        return REASON_TEXT[code] + f" ({AGENT})"
    return REASON_TEXT.get(code)


# Env-var / config names must never reach user text (e.g. RISK_ALLOW_LIVE, allow_live=False).
ENV_NAME_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]*_)+(?:ENABLED|DISABLED|LIVE|KEY|SECRET|TOKEN|MODE|URL)\b(?:\s*=\s*\w+)?"
                         r"|\ballow_live\s*=\s*\w+")
# Check names whose detail only repeats config plumbing: the plain text alone is shown.
NO_DETAIL = frozenset({"trading_mode", "live_disabled", "live_not_enabled"})


def has_env_names(text: str) -> bool:
    return bool(ENV_NAME_RE.search(text or ""))


def plain_reason(reason: Any, keywords: bool = True) -> str:
    """One reason (code, "Blocked (<check>): <detail>", exchange error, or free text) -> plain language naming the limit.

    Numbers from the original detail are kept in parentheses so the exact limit is visible (unless the detail only
    carries config / env-var names). ``keywords=False`` leaves unknown free text as-is (SIM / PAPER wording)."""
    raw = _clean(reason or "")
    if not raw:
        return "no reason given"
    m = _BLOCKED_RE.match(raw)
    if m:
        name, detail = m.group("name"), _clean(m.group("detail"))
        if name in PRICE_IMPACT_CODES:
            pt = price_impact_text(name, detail)
            if pt:
                return pt
        text = _code_text(name, detail) or _keyword_text(detail) or name.replace("_", " ")
        if name in NO_DETAIL or has_env_names(detail):
            detail = ""
        return f"{text} ({detail})" if detail else text
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in REASON_TEXT:
        return _code_text(key, "") or REASON_TEXT[key]
    # "live_token_paused: NVDAB not tradable ..." / "insufficient_data - only 20 bars"
    m2 = re.match(r"^(?P<code>[a-z][a-z0-9_]+)\s*[:\-]\s*(?P<detail>.+)$", raw)
    if m2 and m2.group("code") in REASON_TEXT:
        detail = _clean(m2.group("detail"))
        pt = price_impact_text(m2.group("code"), detail) if m2.group("code") in PRICE_IMPACT_CODES else None
        if pt:
            return pt
        return f"{_code_text(m2.group('code'), detail)} ({detail})"
    m3 = _CODE_RE.match(raw)
    if m3 and int(m3.group("code")) in EXCHANGE_CODE_TEXT:
        rest = _clean(re.sub(r"^[A-Z0-9_]+:\s*", "", m3.group("rest")))
        text = EXCHANGE_CODE_TEXT[int(m3.group("code"))]
        return f"{text} ({rest}; Binance code {m3.group('code')})" if rest else f"{text} (Binance code {m3.group('code')})"
    if _BAD_QUOTE_RE.search(raw) or _UNSIGNED_RE.search(raw) or _ADVERSE_RE.search(raw):
        pt = price_impact_text("", raw)
        if pt:
            return pt
    kw = _keyword_text(raw) if keywords else None
    if has_env_names(raw):
        return kw or "live trading is not enabled (the live-trading switches in Regime's settings are off)"
    return f"{kw} ({raw})" if kw else raw


def _keyword_text(text: str) -> Optional[str]:
    for pat, code in _KEYWORDS:
        if pat.search(text):
            return REASON_TEXT[code]
    return None


def plain_reasons(reasons: Any, keywords: bool = True) -> list[str]:
    out: list[str] = []
    for r in reasons or []:
        t = plain_reason(r, keywords)
        if t not in out:
            out.append(t)
    return out


def handled_codes() -> list[str]:
    return sorted(REASON_TEXT)

"""Runtime-editable LIVE trading limits (dashboard 'Trading limits' panel). Bot 3 private helpers.

The panel's shape (Bot 1) maps onto ``LiveRiskProfile`` fields:

    order_mode          -> order_mode            'fixed' | 'pct'
    order_usdt          -> order_usdt            fixed LIVE BUY size (None = use the strategy's size)
    order_pct           -> order_pct             % of the wallet stablecoin per LIVE BUY (pct mode)
    max_order_usdt      -> max_order_notional_usdt
    max_total_usdt      -> max_open_exposure_usdt
    max_per_token_usdt  -> max_token_exposure_usdt   (None stored = same as max_total_usdt)
    daily_loss_usdt     -> max_daily_loss_usdt   (hitting it triggers the automatic emergency stop)
    daily_buy_usdt      -> max_daily_notional_usdt   (optional; auto-raised to 4 x max_order_usdt when needed)

``apply_live_settings`` validates everything first and returns a NEW profile, so a bad request changes nothing.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from .live import LiveRiskProfile, WalletLimits

SETTING_KEYS = ("order_mode", "order_usdt", "order_pct", "max_order_usdt", "max_total_usdt", "max_per_token_usdt",
                "daily_loss_usdt", "daily_buy_usdt")
# Keys returned by GET that a client may echo back in PUT; they are ignored.
READ_ONLY_KEYS = frozenset({"defaults", "wallet_daily_limit_usdt", "notes"})
DAILY_BUY_AUTO_FACTOR = 4.0

LABELS = {
    "order_usdt": "Order size (USDT)", "order_pct": "Order size (% of wallet)", "max_order_usdt": "Max per order",
    "max_total_usdt": "Max total invested", "max_per_token_usdt": "Max in one token",
    "daily_loss_usdt": "Daily loss limit", "daily_buy_usdt": "Daily buy limit",
}


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def profile_to_settings(lp: LiveRiskProfile) -> dict:
    """The panel's view of a LiveRiskProfile. ``max_per_token_usdt`` is the value in force (falls back to the total)."""
    return {
        "order_mode": lp.order_mode,
        "order_usdt": lp.order_usdt,
        "order_pct": lp.order_pct,
        "max_order_usdt": lp.max_order_notional_usdt,
        "max_total_usdt": lp.max_open_exposure_usdt,
        "max_per_token_usdt": (lp.max_token_exposure_usdt if lp.max_token_exposure_usdt is not None
                               else lp.max_open_exposure_usdt),
        "daily_loss_usdt": lp.max_daily_loss_usdt,
        "daily_buy_usdt": lp.max_daily_notional_usdt,
    }


def _number(key: str, value: Any, errors: list[str], optional: bool = False) -> Optional[float]:
    label = LABELS.get(key, key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        errors.append(f"{label} is required")
        return None
    if isinstance(value, bool):
        errors.append(f"{label} must be a number, not true/false")
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number (got {value!r})")
        return None
    if not math.isfinite(f):
        errors.append(f"{label} must be a finite number")
        return None
    if f <= 0:
        errors.append(f"{label} must be greater than 0 (got {f:g})")
        return None
    return f


def apply_live_settings(lp: LiveRiskProfile, settings: Mapping[str, Any],
                        wallet_limits: Optional[WalletLimits] = None) -> tuple[LiveRiskProfile, list[str]]:
    """Validate ``settings`` (full or partial panel shape) against ``lp`` and return (new profile, notes).

    Missing keys keep their current value. Raises ``ValueError`` with every problem in plain English (joined by
    '; ') and changes nothing if any value is invalid."""
    if not isinstance(settings, Mapping):
        raise ValueError("Trading limits must be an object of setting names to values")
    errors: list[str] = []
    notes: list[str] = []
    unknown = [k for k in settings if k not in SETTING_KEYS and k not in READ_ONLY_KEYS]
    if unknown:
        errors.append("Unknown setting(s): " + ", ".join(sorted(map(str, unknown)))
                      + " (allowed: " + ", ".join(SETTING_KEYS) + ")")

    def given(k: str) -> bool:
        return k in settings

    mode = lp.order_mode
    if given("order_mode"):
        raw = settings["order_mode"]
        m = raw.strip().lower() if isinstance(raw, str) else raw
        if m not in ("fixed", "pct"):
            errors.append(f"Order size mode must be 'fixed' or 'pct' (got {raw!r})")
        else:
            mode = m

    order_usdt = _number("order_usdt", settings["order_usdt"], errors, optional=True) if given("order_usdt") else lp.order_usdt
    order_pct = _number("order_pct", settings["order_pct"], errors, optional=True) if given("order_pct") else lp.order_pct
    max_order = _number("max_order_usdt", settings["max_order_usdt"], errors) if given("max_order_usdt") else lp.max_order_notional_usdt
    max_total = _number("max_total_usdt", settings["max_total_usdt"], errors) if given("max_total_usdt") else lp.max_open_exposure_usdt
    if given("max_per_token_usdt"):
        per_token = _number("max_per_token_usdt", settings["max_per_token_usdt"], errors)
    else:
        per_token = lp.max_token_exposure_usdt  # None = follows max_total_usdt
    daily_loss = _number("daily_loss_usdt", settings["daily_loss_usdt"], errors) if given("daily_loss_usdt") else lp.max_daily_loss_usdt
    daily_buy_given = given("daily_buy_usdt") and settings["daily_buy_usdt"] is not None
    daily_buy = _number("daily_buy_usdt", settings["daily_buy_usdt"], errors) if daily_buy_given else None

    if order_pct is not None and order_pct > 100:
        errors.append(f"{LABELS['order_pct']} must be at most 100 (got {order_pct:g})")
    if mode == "pct" and order_pct is None and not any("order_pct" in e or LABELS["order_pct"] in e for e in errors):
        errors.append(f"{LABELS['order_pct']} is required when the order size mode is 'pct'")

    if errors:  # the cross-field checks below need valid numbers
        raise ValueError("; ".join(errors))

    if max_order > max_total:
        errors.append(f"Max per order ({_fmt(max_order)} USDT) can't be above Max total invested ({_fmt(max_total)} USDT)")
    if per_token is not None and per_token > max_total:
        errors.append(f"Max in one token ({_fmt(per_token)} USDT) can't be above Max total invested ({_fmt(max_total)} USDT)")
    if mode == "fixed" and order_usdt is not None and order_usdt > max_order:
        errors.append(f"Order size ({_fmt(order_usdt)} USDT) can't be above Max per order ({_fmt(max_order)} USDT)")
    if daily_buy is not None and daily_buy < max_order:
        errors.append(f"Daily buy limit ({_fmt(daily_buy)} USDT) can't be below Max per order ({_fmt(max_order)} USDT)")
    wl_daily = wallet_limits.daily_limit_usdt if wallet_limits is not None else None
    if wl_daily is not None and max_order > wl_daily:
        errors.append(f"Max per order ({_fmt(max_order)} USDT) is above the wallet's own daily limit of {_fmt(wl_daily)} "
                      "USDT set in the Binance App. Raise the wallet limit in the app first, or choose a lower value")
    if errors:
        raise ValueError("; ".join(errors))

    if daily_buy is None:
        daily_buy = lp.max_daily_notional_usdt
        if max_order > daily_buy:
            new = max_order * DAILY_BUY_AUTO_FACTOR
            notes.append(f"Daily buy limit raised from {_fmt(daily_buy)} to {_fmt(new)} USDT "
                         f"({DAILY_BUY_AUTO_FACTOR:g} x Max per order) so a full-size order still fits in a day")
            daily_buy = new
    if wl_daily is not None and daily_buy > wl_daily:
        notes.append(f"Daily buy limit {_fmt(daily_buy)} USDT is above the wallet's daily limit of {_fmt(wl_daily)} USDT; "
                     "the wallet limit (the lower one) applies")

    upd = dict(order_mode=mode, order_usdt=order_usdt, order_pct=order_pct, max_order_notional_usdt=max_order,
               max_open_exposure_usdt=max_total, max_token_exposure_usdt=per_token, max_daily_loss_usdt=daily_loss,
               max_daily_notional_usdt=daily_buy)
    try:
        new_lp = LiveRiskProfile(**{**lp.model_dump(), **upd})
    except Exception as e:  # pragma: no cover - every field was validated above
        raise ValueError(f"Invalid trading limits: {e}") from e
    return new_lp, notes


__all__ = ["SETTING_KEYS", "READ_ONLY_KEYS", "DAILY_BUY_AUTO_FACTOR", "apply_live_settings", "profile_to_settings"]

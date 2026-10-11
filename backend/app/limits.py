"""User-set trading limits ("Trading limits" panel): GET/PUT /api/settings/limits, POST /api/settings/limits/reset.

Risk owns the numbers and their validation: with Bot 3's risk (``live_limits_settings`` / ``set_live_limits`` /
``reset_live_limits``) the app only adds the localhost check, persistence (LOG_DIR/limits.json), the alert line and
keeps the engine and the exchange in step. An older risk without those methods gets the same shape and rules from
``FallbackLimits`` below (mapped onto its LiveRiskProfile via set_config).

Shape (Bot 1 / Bot 3, USDT): order_mode "strategy" (default; null = strategy) | "fixed" | "pct"; order_usdt (fixed);
order_pct (% of the wallet, pct); max_order_usdt (per order); max_total_usdt (total invested, all tokens);
max_per_token_usdt (in one token); daily_loss_usdt (automatic emergency stop); daily_buy_usdt (LIVE buys per day).
The wallet's own limits (baw `dailyLimit`, read-only) stay the final cap: risk applies whichever is stricter.
Nothing here ever touches pause or the emergency stop.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from typing import Any, Mapping, Optional

log = logging.getLogger("backend.app.limits")

FIELDS = ("order_mode", "order_usdt", "order_pct", "max_order_usdt", "max_total_usdt", "max_per_token_usdt",
          "daily_loss_usdt", "daily_buy_usdt")
READ_ONLY = frozenset({"wallet_daily_limit_usdt", "defaults", "notes", "is_default", "updated_at"})
ENGINE_DEFAULT_SIZE_PCT = 2.0  # Bot 2 LiveSafetyConfig.max_size_pct (Auto live sizes: never above 2% of equity)


class LimitsError(ValueError):
    """Plain message for a 422."""


def panel(d: Mapping[str, Any]) -> dict:
    return {k: d.get(k) for k in FIELDS}


def has_risk_limits(risk: Any) -> bool:
    return all(callable(getattr(risk, n, None)) for n in ("live_limits_settings", "set_live_limits", "reset_live_limits"))


# ---------------------------------------------------------------- fallback (risk without runtime limits)
class FallbackLimits:
    """Same shape and rules as Bot 3's risk/limits.py, for a risk that cannot change its caps at runtime."""

    def __init__(self, settings: Any, risk: Any) -> None:
        self.risk = risk
        self.defaults = {"order_mode": "strategy", "order_usdt": None, "order_pct": None,
                         "max_order_usdt": float(settings.live_max_order_usdt),
                         "max_total_usdt": float(settings.live_max_exposure_usdt),
                         "max_per_token_usdt": float(settings.live_max_exposure_usdt),
                         "daily_loss_usdt": float(settings.live_max_daily_loss_usdt),
                         "daily_buy_usdt": float(settings.live_max_daily_notional_usdt)}
        self.current = dict(self.defaults)

    def settings(self, wallet_daily: Optional[float]) -> dict:
        return {**self.current, "defaults": dict(self.defaults), "wallet_daily_limit_usdt": wallet_daily, "notes": []}

    def set(self, raw: Mapping[str, Any], wallet_daily: Optional[float]) -> dict:
        d = validate(raw, self.current, wallet_daily)
        self.current = d
        self._apply_risk(d)
        return self.settings(wallet_daily)

    def reset(self, wallet_daily: Optional[float]) -> dict:
        self.current = dict(self.defaults)
        self._apply_risk(self.current)
        return self.settings(wallet_daily)

    def _apply_risk(self, d: dict) -> None:
        cfg = getattr(self.risk, "config", None)
        live = getattr(cfg, "live", None)
        if live is not None and hasattr(live, "max_order_notional_usdt") and callable(getattr(self.risk, "set_config", None)):
            upd = {"max_order_notional_usdt": d["max_order_usdt"], "max_daily_notional_usdt": d["daily_buy_usdt"],
                   "max_open_exposure_usdt": d["max_total_usdt"], "max_daily_loss_usdt": d["daily_loss_usdt"]}
            self.risk.set_config(cfg.model_copy(update={"live": live.model_copy(update=upd)}))  # control untouched


def _num(d: dict, k: str, errors: list[str], optional: bool = False) -> Optional[float]:
    v = d.get(k)
    if v is None or v == "":
        if not optional:
            errors.append(f"{k} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        errors.append(f"{k} must be a number")
        return None
    if float(v) <= 0:
        errors.append(f"{k} must be greater than 0")
        return None
    return float(v)


def validate(raw: Any, base: Mapping[str, Any], wallet_daily: Optional[float]) -> dict:
    if not isinstance(raw, Mapping):
        raise LimitsError("Send the limits as a JSON object.")
    unknown = sorted(set(raw) - set(FIELDS) - READ_ONLY)
    if unknown:
        raise LimitsError(f"Unknown setting(s): {', '.join(unknown)}.")
    d = {**base, **{k: v for k, v in raw.items() if k in FIELDS}}
    mode = d.get("order_mode") or "strategy"
    if mode not in ("strategy", "fixed", "pct"):
        raise LimitsError("Order size mode must be 'strategy', 'fixed' or 'pct'.")
    errors: list[str] = []
    if mode == "strategy":
        d["order_usdt"] = d["order_pct"] = None
    else:
        d["order_usdt"] = _num(d, "order_usdt", errors, optional=mode != "fixed")
        d["order_pct"] = _num(d, "order_pct", errors, optional=mode != "pct")
    for k in ("max_order_usdt", "max_total_usdt", "max_per_token_usdt", "daily_loss_usdt", "daily_buy_usdt"):
        d[k] = _num(d, k, errors)
    if errors:
        raise LimitsError("; ".join(errors))
    if d["order_pct"] is not None and d["order_pct"] > 100:
        errors.append("order_pct must be at most 100")
    if d["max_order_usdt"] > d["max_total_usdt"]:
        errors.append(f"Max per order ({d['max_order_usdt']:g} USDT) can't be above Max total invested ({d['max_total_usdt']:g} USDT)")
    if d["max_per_token_usdt"] > d["max_total_usdt"]:
        errors.append(f"Max in one token ({d['max_per_token_usdt']:g} USDT) can't be above Max total invested ({d['max_total_usdt']:g} USDT)")
    if mode == "fixed" and d["order_usdt"] > d["max_order_usdt"]:
        errors.append(f"Order size ({d['order_usdt']:g} USDT) can't be above Max per order ({d['max_order_usdt']:g} USDT)")
    if wallet_daily is not None and d["max_order_usdt"] > wallet_daily:
        errors.append(f"Max per order ({d['max_order_usdt']:g} USDT) is above the wallet's own daily limit of "
                      f"{wallet_daily:g} USDT set in the Binance App")
    if errors:
        raise LimitsError("; ".join(errors))
    if d["daily_buy_usdt"] < d["max_order_usdt"]:
        d["daily_buy_usdt"] = d["max_order_usdt"] * 4
    d["order_mode"] = mode
    return panel(d)


# ---------------------------------------------------------------- persistence
class LimitsStore:
    """LOG_DIR/limits.json (panel fields only); in memory when LOG_DIR is unset."""

    def __init__(self, log_dir: Optional[str]) -> None:
        self.path = os.path.join(log_dir, "limits.json") if log_dir else None
        self._lock = threading.Lock()

    def load(self) -> Optional[dict]:
        if not self.path or not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return {k: d[k] for k in FIELDS if k in d} if isinstance(d, dict) else None
        except Exception:
            log.exception("limits file %s unreadable; using defaults", self.path)
            return None

    def save(self, d: Optional[dict]) -> None:
        if not self.path:
            return
        with self._lock:
            if d is None:
                if os.path.exists(self.path):
                    os.remove(self.path)
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(panel(d), f, indent=2)
            os.replace(tmp, self.path)


# ---------------------------------------------------------------- engine / exchange / app caps in step with risk
def engine_size_pct(d: Mapping[str, Any]) -> float:
    """Auto mode's live size cap (% of equity): 2% by default; the user's % when it is higher (pct mode), and
    100% for a fixed size (risk sizes the order to order_usdt; the per-order cap still applies)."""
    if d.get("order_mode") == "pct" and d.get("order_pct"):
        return max(ENGINE_DEFAULT_SIZE_PCT, min(100.0, float(d["order_pct"])))
    if d.get("order_mode") == "fixed" and d.get("order_usdt"):
        return 100.0
    return ENGINE_DEFAULT_SIZE_PCT


def apply_engine(engine: Any, d: Mapping[str, Any]) -> str:
    out = "none"
    seen: set[int] = set()
    for eng in (getattr(engine, "_primary", None), getattr(engine, "_fallback", None)):
        fn = getattr(eng, "set_live_limits", None) if eng is not None else None
        if callable(fn) and id(eng) not in seen:
            seen.add(id(eng))
            fn(max_order_usdt=float(d["max_order_usdt"]), max_size_pct=engine_size_pct(d))
            out = "engine.set_live_limits"
    return out


def apply_exchange(exchange: Any, d: Mapping[str, Any]) -> str:
    cap = float(d["max_order_usdt"])
    # exits must always fit: the SELL cap covers a full position in one token (Bot 3 / Bot 4 formula)
    sell = max(2 * cap, float(d.get("max_per_token_usdt") or d.get("max_total_usdt") or 0.0))
    fn = getattr(exchange, "set_live_limits", None)
    if callable(fn):
        from .wiring import supported_kwargs
        kw = supported_kwargs(fn, {"max_order_usdt": cap, "max_sell_usdt": sell})
        fn(**kw) if kw else fn({"max_order_usdt": cap, "max_sell_usdt": sell})
        return "exchange.set_live_limits"
    done = "none"
    ex = getattr(exchange, "executor", None)
    if ex is not None and hasattr(ex, "max_order_usdt"):
        ex.max_order_usdt = cap
        if hasattr(ex, "max_sell_usdt"):  # exits are never squeezed below what they were
            ex.max_sell_usdt = max(float(ex.max_sell_usdt), sell)
        done = "exchange.executor"
    if hasattr(exchange, "max_live_notional"):
        sell = getattr(ex, "max_sell_usdt", None) if ex is not None else None
        exchange.max_live_notional = max(cap, float(sell)) if sell is not None else cap
        done = "exchange.max_live_notional"
    return done


def apply_settings(settings: Any, d: Mapping[str, Any]) -> None:
    """The app's own LIVE caps (used only with a risk that has no LIVE profile) and /api/wallet fallbacks."""
    settings.live_max_order_usdt = float(d["max_order_usdt"])
    settings.live_max_exposure_usdt = float(d["max_total_usdt"])
    settings.live_max_daily_loss_usdt = float(d["daily_loss_usdt"])
    if d.get("daily_buy_usdt"):
        settings.live_max_daily_notional_usdt = float(d["daily_buy_usdt"])


def describe(d: Mapping[str, Any]) -> str:
    mode = d.get("order_mode") or "strategy"
    size = (f"{d['order_pct']:g}% of the wallet per order" if mode == "pct" else
            f"{d['order_usdt']:g} USDT per order" if mode == "fixed" else "the strategy's own order size")
    daily = f", {d['daily_buy_usdt']:g} USDT of buys per day" if d.get("daily_buy_usdt") else ""
    return (f"{size}; max {d['max_order_usdt']:g} per order, {d['max_total_usdt']:g} invested in total, "
            f"{d['max_per_token_usdt']:g} in one token{daily}; daily loss stop {d['daily_loss_usdt']:g} USDT")

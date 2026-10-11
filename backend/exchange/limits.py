"""Runtime LIVE per-order cap (Bot 6's PUT /api/settings/limits -> exchange.set_live_limits(max_order_usdt=...)).

- Default: BINANCE_LIVE_MAX_ORDER_USDT (25) as before; the env value stays the startup default.
- Optional hard ceiling: BINANCE_LIVE_HARD_MAX_ORDER_USDT (unset = no ceiling). A runtime value above it is refused.
- Validation: a finite number > 0 (ValueError otherwise, with a readable message for the API to return as 400).
"""
from __future__ import annotations

import math
import os
from typing import Any, Mapping, Optional

HARD_CEILING_ENV = "BINANCE_LIVE_HARD_MAX_ORDER_USDT"


def hard_max_order_usdt(env: Optional[Mapping[str, str]] = None) -> Optional[float]:
    raw = str((os.environ if env is None else env).get(HARD_CEILING_ENV, "")).strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def validate_max_order_usdt(value: Any, ceiling: Optional[float] = None, name: str = "max_order_usdt") -> float:
    """Return `value` as a float or raise ValueError (not a number, not finite, <= 0, or above `ceiling`)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be > 0 (got {value!r})")
    if ceiling is not None and v > ceiling + 1e-9:
        raise ValueError(f"{name} {v:.2f} is above the hard ceiling {HARD_CEILING_ENV}={ceiling:.2f}")
    return v

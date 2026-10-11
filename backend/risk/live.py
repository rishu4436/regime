"""LIVE (real funds) risk profile and the per-call context the trading loop passes in. Bot 3 private models.

Three independent switches must all be on before a LIVE BUY can be approved:
  1. LIVE_TRADING_ENABLED=true  - backend/app (Bot 6) refuses to start the LIVE loop otherwise
  2. BINANCE_LIVE_ENABLED=true  - backend/exchange (Bot 4) refuses to build the LIVE client otherwise
  3. RiskConfig.allow_live=True - this module; ``RiskConfig.from_env()`` sets it only when RISK_ALLOW_LIVE=true AND the
     two flags above are true. ``DefaultRiskManager()`` without an explicit config uses ``RiskConfig.from_env()``.
Autonomous by design: a LIVE BUY that passes every hard limit is approved without a human, unless the user explicitly
turns confirmation mode on (normal confirmation rules). Protective SELLs are never blocked by a LIVE guard.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.contracts import QUOTE_TTL_SECONDS, Candle, Quote


class LiveRiskProfile(BaseModel):
    """Extra limits applied automatically whenever the trading mode is LIVE. All amounts in USDT unless noted."""

    model_config = ConfigDict(extra="forbid")

    max_order_notional_usdt: float = Field(default=25.0, gt=0, description="Per-order cap (matches BINANCE_LIVE_MAX_NOTIONAL)")
    max_daily_loss_usdt: float = Field(
        default=25.0, gt=0, description="Today's realized + unrealized loss cap; hitting it triggers EMERGENCY STOP")
    max_daily_notional_usdt: float = Field(default=100.0, gt=0, description="Max LIVE BUY notional filled per UTC day")
    max_open_exposure_usdt: float = Field(default=50.0, gt=0, description="Max total value of open positions incl. new order")

    max_data_age_seconds: float = Field(default=900.0, gt=0, description="BUY blocked if the latest price/bar is older")
    max_future_skew_seconds: float = Field(default=60.0, ge=0, description="BUY blocked if the data timestamp is this far in the future")

    max_quote_age_seconds: float = Field(default=float(QUOTE_TTL_SECONDS), gt=0, description="Also blocks expired quotes")
    max_slippage_pct: float = Field(
        default=1.0, gt=0, description="Max |quote price - reference price| / reference, and max quote slippage tolerance")
    require_quote: bool = Field(
        default=False, description="True = LIVE BUY is approved only when a quote is supplied in LiveContext")

    market_hours_enabled: bool = True
    market_open_utc: time = Field(default=time(13, 30), description="US regular session in UTC (EDT). EST is 14:30-21:00")
    market_close_utc: time = time(20, 0)
    market_weekdays_only: bool = True
    market_hours_exempt_symbols: list[str] = Field(default_factory=list, description="Symbols not backed by US stocks")

    require_wallet_balances: bool = Field(default=True, description="LIVE BUY blocked unless LiveContext.balances is supplied")
    quote_asset: str = Field(default="USDT", description="Stablecoin spent on BUYs")
    fee_buffer_usdt: float = Field(default=1.0, ge=0, description="Kept aside from the stablecoin balance for fees")
    gas_asset: str = "BNB"
    min_gas_reserve: float = Field(default=0.002, ge=0, description="Native BNB that must remain for gas (in BNB)")


class LiveContext(BaseModel):
    """Optional facts only the trading loop knows; pass as ``check(..., context=LiveContext(...))``. Used in LIVE;
    in SIM/PAPER it is accepted and ignored except for informational checks."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    now: Optional[datetime] = Field(default=None, description="Decision time (UTC). Default: the manager's clock")
    data_timestamp: Optional[datetime] = Field(
        default=None, description="When the latest price/bar was OBSERVED (fetch time or bar CLOSE time), UTC")
    bar: Optional[Candle] = Field(default=None, description="Latest bar; used if data_timestamp is missing (+ bar_interval)")
    bar_interval: Optional[timedelta] = Field(default=None, description="Bar length; bar close = bar.timestamp + bar_interval")
    reference_price: Optional[float] = Field(default=None, gt=0, description="Mid/last price to compare the quote against")
    quote: Optional[Quote] = None
    balances: Optional[dict[str, float]] = Field(default=None, description="Wallet balances by asset, e.g. {'USDT': 40, 'BNB': 0.01}")
    daily_live_notional_usdt: Optional[float] = Field(
        default=None, ge=0, description="LIVE BUY notional already filled today (UTC), if the app tracks it (survives restarts)")

    def resolved_now(self, fallback: datetime) -> datetime:
        return as_utc(self.now or fallback)

    def resolved_data_time(self) -> Optional[datetime]:
        if self.data_timestamp is not None:
            return as_utc(self.data_timestamp)
        if self.bar is not None:
            return as_utc(self.bar.timestamp + (self.bar_interval or timedelta(0)))
        return None


def as_utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def in_market_hours(now: datetime, p: LiveRiskProfile) -> tuple[bool, str]:
    now = as_utc(now)
    if p.market_weekdays_only and now.weekday() >= 5:
        return False, f"{now:%a %H:%M} UTC is a weekend"
    t = now.time().replace(tzinfo=None)
    ok = p.market_open_utc <= t < p.market_close_utc
    return ok, (f"{now:%a %H:%M} UTC is " + ("inside" if ok else "outside")
                + f" US market hours {p.market_open_utc:%H:%M}-{p.market_close_utc:%H:%M} UTC")

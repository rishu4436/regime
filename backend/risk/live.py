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
from typing import Any, Literal, Mapping, Optional

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
    max_token_exposure_usdt: Optional[float] = Field(default=None, gt=0, description=(
        "Max value held in ONE token incl. the new order. None = same as max_open_exposure_usdt (no extra limit)"))

    # Order sizing set by the user (dashboard 'Trading limits' panel). Default = the strategy's own size (unchanged).
    order_mode: Literal["fixed", "pct"] = Field(default="fixed", description=(
        "'fixed' = order_usdt per LIVE BUY (None = use the strategy's size); 'pct' = order_pct % of the wallet stablecoin"))
    order_usdt: Optional[float] = Field(default=None, gt=0, description="Fixed LIVE BUY size in USDT (fixed mode)")
    order_pct: Optional[float] = Field(default=None, gt=0, le=100, description="LIVE BUY size as % of wallet USDT (pct mode)")

    max_data_age_seconds: float = Field(default=900.0, gt=0, description="BUY blocked if the latest price/bar is older")
    max_future_skew_seconds: float = Field(default=60.0, ge=0, description="BUY blocked if the data timestamp is this far in the future")

    max_quote_age_seconds: float = Field(default=float(QUOTE_TTL_SECONDS), gt=0, description="Also blocks expired quotes")
    max_slippage_pct: float = Field(
        default=1.0, gt=0, description=(
            "Max ADVERSE price impact (quote worse than the reference price, or the exchange-reported impact), and max "
            "quote slippage tolerance"))
    max_favorable_impact_pct: float = Field(default=3.0, gt=0, description=(
        "Sanity cap on a FAVORABLE quote (better than the reference price). Small favorable gaps are allowed; beyond this "
        "the quote is probably bad data, so the BUY is blocked"))
    require_quote: bool = Field(
        default=False, description="True = LIVE BUY is approved only when a quote is supplied in LiveContext")

    market_hours_enabled: bool = False  # off by default: on-chain tokenized stocks trade 24/7
    market_open_utc: time = Field(default=time(13, 30), description="US regular session in UTC (EDT). EST is 14:30-21:00")
    market_close_utc: time = time(20, 0)
    market_weekdays_only: bool = True
    market_hours_exempt_symbols: list[str] = Field(default_factory=list, description="Symbols not backed by US stocks")

    require_wallet_balances: bool = Field(default=True, description="LIVE BUY blocked unless LiveContext.balances is supplied")
    quote_asset: str = Field(default="USDT", description="Stablecoin spent on BUYs")
    fee_buffer_usdt: float = Field(default=1.0, ge=0, description="Kept aside from the stablecoin balance for fees")
    gas_asset: str = "BNB"
    min_gas_reserve: float = Field(default=0.002, ge=0, description="Native BNB that must remain for gas (in BNB)")

    blocked_symbol_suffixes: list[str] = Field(
        default_factory=lambda: ["x"], description="Case-sensitive symbol suffixes never bought in LIVE (xStocks on BSC: no liquidity)")
    reserve_wallet_quota_for_exits: bool = Field(
        default=True, description="Shrink LIVE BUYs so the wallet's daily quota (which counts SELLs) can still sell everything")
    require_wallet_limits: bool = Field(
        default=False, description="True = LIVE BUY blocked unless LiveContext.wallet_limits (the wallet's own caps) is supplied")
    bump_to_min_order: bool = Field(default=True, description=(
        "LIVE only: a sized BUY below the effective minimum (max(min_order_notional, token min_order_usdt)) is raised to "
        "that minimum when it still fits every hard ceiling (per-order caps, wallet balance - fee buffer, LIVE exposure, "
        "daily notional, wallet daily quota and exit reserve, cash). It may override the %-of-equity size/exposure rules, "
        "never a hard cap"))
    use_pct_daily_loss_in_live: bool = Field(default=False, description=(
        "False = in LIVE the %-of-capital daily_loss_limit check is skipped (shown as passed) because the absolute "
        "max_daily_loss_usdt cap governs; True = enforce both"))


# Key names accepted by WalletLimits.from_baw. DOCUMENTED = in the official `baw wallet settings --json` reference
# (binance-skills-hub skills/binance-web3/binance-agentic-wallet/references/wallet-setting.md). Everything else is a
# PROVISIONAL alias: tolerated in case a CLI version / wrapper returns it, not backed by any doc.
_BAW_KEYS: dict[str, tuple[str, ...]] = {
    # documented: dailyLimit, quotaUsed, quotaLeft, quotaDate, tradeAllTokens
    "daily_limit_usdt": ("dailyLimit", "daily_limit_usdt", "daily_limit", "dailyLimitUsd", "dailyQuota"),
    "daily_used_usdt": ("quotaUsed", "daily_used_usdt", "dailyUsed", "daily_used", "usedQuota"),
    "daily_left_usdt": ("quotaLeft", "daily_left_usdt", "leftQuota", "left_quota", "remainingQuota", "dailyLeft"),
    "quota_date": ("quotaDate", "quota_date"),
    "trade_all_tokens": ("tradeAllTokens", "trade_all_tokens"),
    # provisional: baw documents NO per-transaction limit and NO allow-list in `wallet settings`
    "per_tx_limit_usdt": ("perTxLimit", "per_tx_limit_usdt", "perTransactionLimit", "singleTxLimit", "maxPerTx",
                          "singleTransactionLimit", "per_tx_limit"),
    "allowed_tokens": ("allowedTokens", "allowed_tokens", "tokenWhitelist", "whitelistTokens", "tradableTokens"),
    "allowed_contracts": ("allowedContracts", "allowed_contracts", "contractWhitelist", "whitelistContracts"),
}


class TokenStatus(BaseModel):
    """Per-token tradability (e.g. from Bot 4's RWA asset-status endpoint). LIVE BUY needs trading_status == 'trading'
    and reference_price_only == False."""

    model_config = ConfigDict(extra="ignore")

    trading_status: str = Field(default="trading", description="'trading', 'offhours', 'closed', 'no_liquidity', ...")
    min_order_usdt: Optional[float] = Field(default=None, ge=0, description="Token minimum order (Ondo: 20 USD)")
    reference_price_only: bool = Field(default=False, description="True = price shown is a reference, not executable")

    def is_trading(self) -> bool:
        return self.trading_status.strip().lower() == "trading" and not self.reference_price_only


class WalletLimits(BaseModel):
    """The Agentic Wallet's OWN spending controls (set in the Binance App; ``baw`` can only read them). Risk applies
    whichever is stricter, these or LiveRiskProfile, so a BUY is shrunk/blocked before the wallet would reject it.
    Every field is optional; ``None`` = unknown / no restriction from the wallet side. Amounts in USDT (baw reports USD;
    treated 1:1)."""

    model_config = ConfigDict(extra="forbid")

    per_tx_limit_usdt: Optional[float] = Field(default=None, ge=0, description="Max value of a single transaction")
    daily_limit_usdt: Optional[float] = Field(default=None, ge=0, description="baw `dailyLimit`: max total tx value per day")
    daily_used_usdt: Optional[float] = Field(default=None, ge=0, description="baw `quotaUsed`: consumed today")
    daily_left_usdt: Optional[float] = Field(default=None, ge=0, description="baw `quotaLeft`: remaining today")
    quota_date: Optional[str] = Field(default=None, description="baw `quotaDate` the quota figures apply to")
    trade_all_tokens: Optional[bool] = Field(
        default=None, description="baw `tradeAllTokens`; False = only an App-side allow-list (not returned by baw)")
    allowed_tokens: Optional[list[str]] = Field(
        default=None, description="Symbols or contract addresses (case-insensitive). None = no restriction known")
    allowed_contracts: Optional[list[str]] = Field(default=None, description="Informational only")
    source: Optional[str] = Field(default=None, description="e.g. 'baw'")
    fetched_at: Optional[datetime] = None

    def daily_remaining_usdt(self) -> Optional[float]:
        """Wallet daily headroom: min(dailyLimit - quotaUsed, quotaLeft) over the values present, floored at 0."""
        vals = []
        if self.daily_limit_usdt is not None:
            vals.append(self.daily_limit_usdt - (self.daily_used_usdt or 0.0))
        if self.daily_left_usdt is not None:
            vals.append(self.daily_left_usdt)
        return max(0.0, min(vals)) if vals else None

    def allows(self, *identifiers: Optional[str]) -> bool:
        """True if no allow-list is set, or any non-empty identifier (symbol / address) is in it (case-insensitive)."""
        if self.allowed_tokens is None:
            return True
        allowed = {t.strip().lower() for t in self.allowed_tokens}
        return any(i and i.strip().lower() in allowed for i in identifiers)

    @classmethod
    def from_baw(cls, payload: Mapping[str, Any], *, source: str = "baw", fetched_at: Optional[datetime] = None,
                 allowed_tokens: Optional[list[str]] = None) -> "WalletLimits":
        """Map ``baw wallet settings --json`` output (the full ``{"success", "data": {...}}`` envelope or just ``data``).

        Documented keys: dailyLimit, quotaUsed, quotaLeft, quotaDate, tradeAllTokens. baw does NOT document a
        per-transaction limit or return the token allow-list, so those are read only from PROVISIONAL aliases (see
        ``_BAW_KEYS``) or passed explicitly via ``allowed_tokens`` (e.g. from config). Unknown keys are ignored; numbers
        given as strings are parsed; unparsable numbers become None."""
        data: Mapping[str, Any] = payload or {}
        if fetched_at is None:  # Bot 4's get_wallet_settings() adds fetched_at next to the raw baw JSON
            raw_t = data.get("fetched_at") or (data.get("data") or {}).get("fetched_at") if isinstance(data.get("data"), Mapping) else data.get("fetched_at")
            if isinstance(raw_t, datetime):
                fetched_at = raw_t
            elif isinstance(raw_t, str):
                try:
                    fetched_at = datetime.fromisoformat(raw_t.replace("Z", "+00:00"))
                except ValueError:
                    fetched_at = None
        if isinstance(data.get("data"), Mapping):
            data = data["data"]

        def pick(field: str) -> Any:
            for k in _BAW_KEYS[field]:
                if k in data and data[k] is not None:
                    return data[k]
            return None

        def num(field: str) -> Optional[float]:
            v = pick(field)
            if isinstance(v, bool):
                return None
            try:
                f = float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
            return f if f is not None and f >= 0 else None

        def strs(v: Any) -> Optional[list[str]]:
            if v is None:
                return None
            if isinstance(v, str):
                v = [x for x in v.replace(";", ",").split(",")]
            out = []
            for x in v:
                if isinstance(x, Mapping):  # e.g. {"symbol": .., "contractAddress": ..}
                    out += [str(x[k]) for k in ("symbol", "contractAddress", "address", "tokenAddress") if x.get(k)]
                elif x is not None and str(x).strip():
                    out.append(str(x).strip())
            return out

        tat = pick("trade_all_tokens")
        if isinstance(tat, str):
            tat = {"true": True, "false": False}.get(tat.strip().lower())
        tokens = allowed_tokens if allowed_tokens is not None else strs(pick("allowed_tokens"))
        if tat is True and allowed_tokens is None:
            tokens = None  # wallet may trade any token
        qd = pick("quota_date")
        return cls(per_tx_limit_usdt=num("per_tx_limit_usdt"), daily_limit_usdt=num("daily_limit_usdt"),
                   daily_used_usdt=num("daily_used_usdt"), daily_left_usdt=num("daily_left_usdt"),
                   quota_date=str(qd) if qd is not None else None,
                   trade_all_tokens=tat if isinstance(tat, bool) else None, allowed_tokens=tokens,
                   allowed_contracts=strs(pick("allowed_contracts")), source=source, fetched_at=fetched_at)


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
    token_status: Optional[TokenStatus] = Field(
        default=None, description="Token tradability; a dict like {'trading_status': 'trading', 'min_order_usdt': 20} also works")
    price_impact_pct: Optional[float] = Field(
        default=None, description="Exchange-reported price impact in % (Web3 quote `priceImpactPercent`); used by live_slippage")
    wallet_limits: Optional[WalletLimits] = Field(
        default=None, description="Agentic Wallet's own caps, e.g. WalletLimits.from_baw(<baw wallet settings --json>)")

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

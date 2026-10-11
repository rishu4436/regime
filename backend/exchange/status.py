"""Per-token trading status, minimum order size, shares multiplier / reference price and quote price impact,
plus a non-exception "why can't I trade this now" path (QuoteResult) for the loop (Bot 6), risk (Bot 3) and
the engine (Bot 2).

Evidence (all fetched Sun 2026-10-11, saved by Bot 2 in /workspace/bot2-notes/samples/):
- Public RWA status, `GET www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai`
  (status_NVDAon.json) and `statusInfo` inside `.../v2/.../rwa/dynamic/ai` (rwa_dynamic_*.json):
  `{openState, marketStatus, reasonCode, reasonMsg, nextOpenTime, nextCloseTime}`.
  NVDAon -> openState=true, marketStatus="offhours", reasonCode="TRADING" (Ondo off-hours session, weekend).
  NVDAB  -> openState=true, marketStatus=null, reasonCode="TRADING" (bStock trades 24/7).
  Documented reasonCodes (binance-tokenized-securities-info SKILL.md): TRADING, MARKET_CLOSED, MARKET_PAUSED,
  ASSET_PAUSED (corporate action), ASSET_LIMITED (earnings), MAINTENANCE.
- `rwa/dynamic/ai` -> `tokenInfo.price`, `tokenInfo.sharesMultiplier`, `stockInfo.price` (underlying, null off-hours
  for bStock), `limitInfo.maxActiveNotionalValue`. SKILL.md: referencePrice = tokenInfo.price / sharesMultiplier.
- HMAC Web3 API `rwa/price` -> `tokenPrice`, `referencePrice` (Bot 4 research), quote routes -> `priceImpactPercent`.
- K-lines: Ondo weekend bars have volume "0" (kline_NVDAon_1h.json); TSLAx (xStock) newest bar is a week old and
  token dynamic info shows `liquidity: "0"` (kline_TSLAx_1h.json, dyn_TSLAx.json).

ASSUMPTIONS (no field in any sample):
- No API field gives a minimum order size. Ondo's 20 USD minimum comes from Trading API error 40375
  ONDO_FROM_USD_AMOUNT_TOO_SMALL ("Minimum order amount is 20 USD"); everything else uses the team default of
  10 USDT (gas/fee floor agreed with Bot 3). `limitInfo.maxActiveNotionalValue` is a *maximum*, reported separately.
- If no status payload is available, status is derived from candles: newest bar older than STALE_AFTER -> NO_LIQUIDITY,
  last ZERO_VOLUME_BARS bars all zero-volume -> OFFHOURS (reference-price-only), otherwise TRADING.
- price_impact_pct: route `priceImpactPercent` (percent, e.g. "0.0001" = 0.0001%) when present; otherwise computed as
  the quote price vs the current mid/token price (positive = worse for us); None when neither is known.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from backend.contracts import Candle, OrderSide, Quote, TokenType, utc_now

from .errors import (
    BSTOCK_INVALID_TRADING_TIME,
    ONDO_FROM_USD_AMOUNT_TOO_SMALL,
    ONDO_MARKET_STATE_NOT_TRADABLE,
    ONDO_MAX_SINGLE_ORDER_LIMIT,
    RWA_INSUFFICIENT_LIQUIDITY,
    QuoteUnavailable,
)

log = logging.getLogger(__name__)


class TradingStatus(str, Enum):
    TRADING = "TRADING"          # live order book / AMM, real volume
    OFFHOURS = "OFFHOURS"        # quotable but reference-price-only (Ondo weekend session, zero-volume candles)
    CLOSED = "CLOSED"            # market closed / paused / corporate action / maintenance
    NO_LIQUIDITY = "NO_LIQUIDITY"  # no pool liquidity / dead token (xStock on BSC)
    UNKNOWN = "UNKNOWN"


class QuoteReason(str, Enum):
    MARKET_CLOSED = "market_closed"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    BELOW_MINIMUM = "below_minimum"
    XSTOCK_DISABLED = "xstock_disabled"
    ABOVE_MAXIMUM = "above_maximum"  # 40366 ONDO_MAX_SINGLE_ORDER_LIMIT


DEFAULT_MIN_ORDER_USDT = 10.0
ONDO_MIN_ORDER_USDT = 20.0  # 40375 ONDO_FROM_USD_AMOUNT_TOO_SMALL
STALE_AFTER = timedelta(days=3)
ZERO_VOLUME_BARS = 6

ERROR_CODE_REASONS: dict[int, QuoteReason] = {
    ONDO_MARKET_STATE_NOT_TRADABLE: QuoteReason.MARKET_CLOSED,   # 40367
    BSTOCK_INVALID_TRADING_TIME: QuoteReason.MARKET_CLOSED,      # 40369
    RWA_INSUFFICIENT_LIQUIDITY: QuoteReason.INSUFFICIENT_LIQUIDITY,  # 40374
    ONDO_FROM_USD_AMOUNT_TOO_SMALL: QuoteReason.BELOW_MINIMUM,   # 40375
    ONDO_MAX_SINGLE_ORDER_LIMIT: QuoteReason.ABOVE_MAXIMUM,      # 40366
}
STATUS_REASONS = {TradingStatus.CLOSED: QuoteReason.MARKET_CLOSED,
                  TradingStatus.NO_LIQUIDITY: QuoteReason.INSUFFICIENT_LIQUIDITY}
CLOSED_REASON_CODES = {"MARKET_CLOSED", "MARKET_PAUSED", "ASSET_PAUSED", "MAINTENANCE", "MARKET_MAINTENANCE",
                       "UNSUPPORTED"}
CLOSED_MARKET_STATUSES = {"closed", "pause", "paused"}
OFFHOURS_MARKET_STATUSES = {"offhours"}


def reason_for_error_code(code: Optional[int]) -> Optional[str]:
    r = ERROR_CODE_REASONS.get(code) if code is not None else None
    return r.value if r else None


def xstock_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """xStock (BSC, symbols ending in 'x') is off unless BINANCE_ENABLE_XSTOCK=true: zero liquidity measured."""
    return (env if env is not None else os.environ).get("BINANCE_ENABLE_XSTOCK", "").strip().lower() == "true"


def is_xstock(symbol: str, token_type: Optional[TokenType] = None) -> bool:
    return token_type == TokenType.XSTOCK or (token_type in (None, TokenType.OTHER) and symbol.endswith("x"))


def default_min_order_usdt(token_type: TokenType) -> float:
    return ONDO_MIN_ORDER_USDT if token_type == TokenType.ONDO else DEFAULT_MIN_ORDER_USDT


def _f(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).strip().rstrip("%"))
    except ValueError:
        return None


def _ms(v: Any) -> Optional[datetime]:
    x = _f(v)
    return datetime.fromtimestamp(x / 1000, tz=timezone.utc) if x else None


def status_from_status_info(info: Optional[Mapping[str, Any]]) -> TradingStatus:
    """Map `{openState, marketStatus, reasonCode}` (asset market status / rwa dynamic statusInfo) to TradingStatus."""
    if not info:
        return TradingStatus.UNKNOWN
    reason = str(info.get("reasonCode") or "").upper()
    market = str(info.get("marketStatus") or "").lower()
    if info.get("openState") is False or reason in CLOSED_REASON_CODES or market in CLOSED_MARKET_STATUSES:
        return TradingStatus.CLOSED
    if market in OFFHOURS_MARKET_STATUSES:
        return TradingStatus.OFFHOURS
    if info.get("openState") is True or reason in ("TRADING", "ASSET_LIMITED"):
        return TradingStatus.TRADING
    return TradingStatus.UNKNOWN


def status_from_candles(candles: Sequence[Candle], now: Optional[datetime] = None,
                        stale_after: timedelta = STALE_AFTER, zero_volume_bars: int = ZERO_VOLUME_BARS) -> TradingStatus:
    """Fallback when no status payload exists (see module ASSUMPTIONS)."""
    if not candles:
        return TradingStatus.UNKNOWN
    now = now or utc_now()
    if now - candles[-1].timestamp > stale_after:
        return TradingStatus.NO_LIQUIDITY
    tail = list(candles)[-zero_volume_bars:]
    if len(tail) >= min(zero_volume_bars, len(candles)) and all(c.volume <= 0 for c in tail):
        return TradingStatus.OFFHOURS
    return TradingStatus.TRADING


def parse_rwa_dynamic(data: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """`rwa/dynamic/ai` data -> {price, shares_multiplier, reference_price, max_notional_usdt, status_info}."""
    data = data or {}
    tok, stock = data.get("tokenInfo") or {}, data.get("stockInfo") or {}
    price, mult = _f(tok.get("price")), _f(tok.get("sharesMultiplier"))
    ref = _f(stock.get("price"))
    if ref is None and price and mult:
        ref = price / mult  # SKILL.md: referencePrice = tokenInfo.price / sharesMultiplier
    return {"price": price, "shares_multiplier": mult, "reference_price": ref,
            "max_notional_usdt": _f((data.get("limitInfo") or {}).get("maxActiveNotionalValue")),
            "status_info": data.get("statusInfo")}


def price_impact_pct(route: Optional[Mapping[str, Any]], side: OrderSide, quote_price: Optional[float],
                     mid_price: Optional[float]) -> Optional[float]:
    if route and _f(route.get("priceImpactPercent")) is not None:
        return _f(route.get("priceImpactPercent"))
    if not quote_price or not mid_price or mid_price <= 0:
        return None
    pct = (quote_price / mid_price - 1) * 100 if side == OrderSide.BUY else (1 - quote_price / mid_price) * 100
    return round(pct, 6)


@dataclass
class TokenStatus:
    symbol: str
    token_type: str
    trading_status: TradingStatus
    tradable: bool                       # the venue should accept a quote now (OFFHOURS is quotable)
    min_order_usdt: float
    reason: Optional[str] = None         # QuoteReason value when not tradable
    shares_multiplier: Optional[float] = None
    reference_price: Optional[float] = None  # underlying price per share
    price: Optional[float] = None            # on-chain token price (USDT)
    max_order_usdt: Optional[float] = None   # limitInfo.maxActiveNotionalValue when present
    market_status: Optional[str] = None      # raw marketStatus (offhours, regular, ...)
    reason_code: Optional[str] = None        # raw reasonCode (TRADING, ASSET_PAUSED, ...)
    reason_msg: Optional[str] = None
    next_open_time: Optional[datetime] = None
    next_close_time: Optional[datetime] = None
    source: str = "default"                  # sim | rwa_dynamic | candles | policy | default
    checked_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trading_status"] = self.trading_status.value
        for k in ("next_open_time", "next_close_time", "checked_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def finalize_status(st: TokenStatus, xstock_on: bool) -> TokenStatus:
    """Fill tradable/reason consistently (xStock policy first)."""
    if is_xstock(st.symbol, TokenType(st.token_type)) and not xstock_on:
        st.tradable, st.reason = False, QuoteReason.XSTOCK_DISABLED.value
    else:
        r = STATUS_REASONS.get(st.trading_status)
        st.tradable, st.reason = r is None, (r.value if r else None)
    return st


def apply_status_info(st: TokenStatus, info: Optional[Mapping[str, Any]]) -> TokenStatus:
    if info:
        st.trading_status = status_from_status_info(info)
        st.market_status, st.reason_code, st.reason_msg = info.get("marketStatus"), info.get("reasonCode"), info.get("reasonMsg")
        st.next_open_time, st.next_close_time = _ms(info.get("nextOpenTime")), _ms(info.get("nextCloseTime"))
    return st


@dataclass
class QuoteResult:
    """Non-exception quote outcome. ok=True -> `quote` is a normal contract Quote, ready for execute_swap/rfq."""
    ok: bool
    symbol: str
    side: OrderSide
    notional: float
    quote: Optional[Quote] = None
    reason: Optional[str] = None          # QuoteReason value when ok=False
    message: Optional[str] = None
    error_code: Optional[int] = None      # Binance code when the API refused (40367/40369/40374/40375/40366)
    trading_status: TradingStatus = TradingStatus.UNKNOWN
    min_order_usdt: float = DEFAULT_MIN_ORDER_USDT
    price_impact_pct: Optional[float] = None
    reference_price: Optional[float] = None
    shares_multiplier: Optional[float] = None
    token_status: Optional[TokenStatus] = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "symbol": self.symbol, "side": self.side.value, "notional": self.notional,
                "quote": self.quote.model_dump(mode="json") if self.quote else None, "reason": self.reason,
                "message": self.message, "error_code": self.error_code, "trading_status": self.trading_status.value,
                "min_order_usdt": self.min_order_usdt, "price_impact_pct": self.price_impact_pct,
                "reference_price": self.reference_price, "shares_multiplier": self.shares_multiplier,
                "token_status": self.token_status.to_dict() if self.token_status else None}


class TokenStatusMixin:
    """Shared `quote_or_reason` / `quote_meta` for SIM and PAPER/LIVE. Needs get_token_status, get_quote and
    optionally `_raw_route(quote_id)` returning the API route dict."""

    _quote_meta: dict[str, QuoteResult]

    def get_token_status(self, symbol: str) -> TokenStatus:  # pragma: no cover - implemented by clients
        raise NotImplementedError

    def _raw_route(self, quote_id: str) -> Optional[Mapping[str, Any]]:
        return None

    def quote_meta(self, quote_id: str) -> Optional[QuoteResult]:
        """Status / min order / price impact for a Quote returned by get_quote or quote_or_reason."""
        return getattr(self, "_quote_meta", {}).get(quote_id)

    def quote_or_reason(self, symbol: str, side: OrderSide, notional: float) -> QuoteResult:
        """Like get_quote but never raises for market reasons: returns QuoteResult(ok=False, reason=...).
        Still raises QuoteUnavailable without a reason (unknown token, missing wallet, no route config) and any
        other ExchangeError (auth, region, rate limit) - those are bugs/config, not market state."""
        st = self.get_token_status(symbol)
        base = dict(symbol=symbol, side=side, notional=notional, trading_status=st.trading_status,
                    min_order_usdt=st.min_order_usdt, reference_price=st.reference_price,
                    shares_multiplier=st.shares_multiplier, token_status=st)

        def fail(reason: str, msg: str, code: Optional[int] = None) -> QuoteResult:
            return QuoteResult(ok=False, reason=reason, message=msg, error_code=code, **base)

        if st.reason == QuoteReason.XSTOCK_DISABLED.value:
            return fail(st.reason, f"{symbol}: xStock disabled (zero BSC liquidity); set BINANCE_ENABLE_XSTOCK=true")
        if not st.tradable and st.reason:
            return fail(st.reason, f"{symbol}: {st.trading_status.value}"
                        + (f" ({st.reason_code}{'/' + st.reason_msg if st.reason_msg else ''})" if st.reason_code else ""))
        if side == OrderSide.BUY and notional < st.min_order_usdt:
            return fail(QuoteReason.BELOW_MINIMUM.value,
                        f"{symbol}: {notional:.2f} USDT is below the {st.min_order_usdt:.0f} USDT minimum order")
        if side == OrderSide.BUY and st.max_order_usdt and notional > st.max_order_usdt:
            return fail(QuoteReason.ABOVE_MAXIMUM.value, f"{symbol}: {notional:.2f} USDT above {st.max_order_usdt:.0f}")
        try:
            q = self.get_quote(symbol, side, notional)  # type: ignore[attr-defined]
        except QuoteUnavailable as e:
            if not e.reason:
                raise
            return fail(e.reason, str(e), e.code)
        res = self.quote_meta(q.quote_id) or QuoteResult(ok=True, **base)
        res.quote, res.ok = q, True
        return res

    def _record_quote_meta(self, q: Quote, st: TokenStatus, mid: Optional[float],
                           route: Optional[Mapping[str, Any]] = None) -> QuoteResult:
        if not hasattr(self, "_quote_meta"):
            self._quote_meta = {}
        res = QuoteResult(ok=True, symbol=q.symbol, side=q.side, notional=q.notional, quote=q,
                          trading_status=st.trading_status, min_order_usdt=st.min_order_usdt,
                          price_impact_pct=price_impact_pct(route, q.side, q.price, mid),
                          reference_price=st.reference_price, shares_multiplier=st.shares_multiplier, token_status=st)
        if len(self._quote_meta) > 1000:
            self._quote_meta.clear()
        self._quote_meta[q.quote_id] = res
        return res

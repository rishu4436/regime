"""Full tokenized-stock universe on BSC: every bStock (…B), Ondo (…on) and xStock (…x).

Sources (verified 2026-10-11, samples in tests/samples/):
- `GET /api/v1/dex/market/rwa/tokens?binanceChainId=56` (HMAC Web3 API, "Get RWA Token List", read-only GET from
  Rishu's PC at ~11:50 IST): 488 rows = 442 Ondo + 46 bStock, with tokenName, tokenSymbol, decimals,
  underlyingTicker, tokenToShareRatio, statusInfo{openState, marketStatus, reasonCode}, tokenPrice, referencePrice,
  volume24H (the *underlying US stock* volume in USD, not DEX volume), marketCap, tags (["alpha"] or []).
  No xStocks. `platformId` (ondo|bstock) filters; `tabId` and `page`/`pageSize` were ignored (same 488 rows), so the
  API gives no sector/narrative and we don't invent one (Bot 5 owns the narrative map).
- `POST /api/v1/dex/market/price-info` (HMAC, batch <= 100 `{binanceChainId, tokenContractAddress}`): on-chain
  `price`, `buyVolume24H` + `sellVolume24H` (the DEX volume we report as volume_24h) and `liquidity` (USD, mostly
  null for RWA tokens: 1 of 100 rows had it).
- Public no-key list `GET www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai`
  (same rows as `.../stock/scaleui/list` that `baw` uses): every deployment, `type` 1 Ondo / 2 xStock / 3 bStock /
  4+ other. On BSC: 458 Ondo, 130 xStock, 91 bStock. Used for xStocks and for tokens the HMAC list doesn't carry
  (e.g. AAPLB: listed by baw, absent from rwa/tokens). Those rows get status UNKNOWN and tradable=False ("unlisted").

Row keys (what Bot 2's Auto-pick and the UI read; snake_case):
  symbol, name, type ("bStock" | "Ondo" | "xStock"), token_type (contract TokenType value), chain, chain_id,
  contract_address, decimals, underlying, price, volume_24h, liquidity, min_order_usdt, max_order_usdt,
  status (TRADING | OFFHOURS | CLOSED | NO_LIQUIDITY | UNKNOWN), tradable (bool), reason,
  plus extras: reference_price, shares_multiplier, underlying_volume_24h, market_cap, market_status, reason_code,
  next_open_time, tags, source ("rwa_tokens" | "public_list" | "registry" | "sim").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from backend.contracts import TokenInfo, TokenType

from .status import (
    QuoteReason,
    TokenStatus,
    TradingStatus,
    _f,
    _ms,
    default_min_order_usdt,
    finalize_status,
    status_from_status_info,
)
from .tokens import BSC, CHAIN_NAMES, infer_token_type

log = logging.getLogger(__name__)

UNIVERSE_TTL_S = 600.0          # ~10 minutes
PRICE_INFO_BATCH = 100          # /market/price-info batch limit
LIST_TYPES = {1: TokenType.ONDO, 2: TokenType.XSTOCK, 3: TokenType.BSTOCK}   # public list `type`
TYPE_LABELS = {TokenType.BSTOCK: "bStock", TokenType.ONDO: "Ondo", TokenType.XSTOCK: "xStock"}
UNLISTED_REASON = "unlisted"    # in the public list but not in the Web3 API list: no status, not offered for trading


def _iso(v: Any) -> Optional[str]:
    d = _ms(v)
    return d.isoformat() if d else None


def _row(*, symbol: str, name: str, token_type: TokenType, chain_id: str, contract: str, decimals: int,
         underlying: Optional[str], source: str) -> dict[str, Any]:
    return {
        "symbol": symbol, "name": name, "type": TYPE_LABELS.get(token_type, token_type.value),
        "token_type": token_type.value, "chain": CHAIN_NAMES.get(chain_id, chain_id), "chain_id": chain_id,
        "contract_address": contract, "decimals": decimals, "underlying": underlying,
        "price": None, "volume_24h": None, "liquidity": None,
        "min_order_usdt": default_min_order_usdt(token_type), "max_order_usdt": None,
        "status": TradingStatus.UNKNOWN.value, "tradable": False, "reason": None,
        "reference_price": None, "shares_multiplier": None, "underlying_volume_24h": None, "market_cap": None,
        "market_status": None, "reason_code": None, "next_open_time": None, "tags": [], "source": source,
    }


def _finish(row: dict[str, Any], status: TradingStatus, enable_xstock: bool) -> dict[str, Any]:
    """Same tradable/reason rules as get_token_status (finalize_status): xStock policy first."""
    st = finalize_status(TokenStatus(row["symbol"], row["token_type"], status, True, row["min_order_usdt"]),
                         enable_xstock)
    if row["token_type"] == TokenType.XSTOCK.value and not enable_xstock:
        status = TradingStatus.NO_LIQUIDITY
    row["status"], row["tradable"], row["reason"] = status.value, st.tradable, st.reason
    return row


def rows_from_rwa_tokens(data: Iterable[Mapping[str, Any]], chain_id: str = BSC,
                         enable_xstock: bool = False) -> list[dict[str, Any]]:
    """`/market/rwa/tokens` data -> universe rows (only `chain_id`, only Ondo / bStock / xStock)."""
    out: list[dict[str, Any]] = []
    for a in data or []:
        sym, addr = a.get("tokenSymbol"), a.get("tokenContractAddress")
        if not sym or not addr or str(a.get("binanceChainId") or chain_id) != chain_id:
            continue
        tt = infer_token_type(sym, a.get("platformId"))
        if tt not in TYPE_LABELS:
            continue
        try:
            dec = int(a.get("decimals") or 18)
        except (TypeError, ValueError):
            dec = 18
        row = _row(symbol=sym, name=a.get("tokenName") or a.get("underlyingName") or sym, token_type=tt,
                   chain_id=chain_id, contract=str(addr).lower(), decimals=dec, underlying=a.get("underlyingTicker"),
                   source="rwa_tokens")
        info = a.get("statusInfo") or {}
        row.update(price=_f(a.get("tokenPrice")), reference_price=_f(a.get("referencePrice")),
                   shares_multiplier=_f(a.get("tokenToShareRatio")), underlying_volume_24h=_f(a.get("volume24H")),
                   market_cap=_f(a.get("marketCap")), market_status=info.get("marketStatus"),
                   reason_code=info.get("reasonCode"), next_open_time=_iso(info.get("nextOpenTime")),
                   tags=list(a.get("tags") or []))
        out.append(_finish(row, status_from_status_info(info) if info else TradingStatus.UNKNOWN, enable_xstock))
    return out


def rows_from_public_list(data: Iterable[Mapping[str, Any]], chain_id: str = BSC,
                          enable_xstock: bool = False, unlisted: bool = True) -> list[dict[str, Any]]:
    """Public `rwa/stock/detail/list/ai` data -> rows (no name/price/status: name = "<ticker> (<type>)").
    unlisted=True (the Web3 API list loaded and these rows are missing from it): Ondo/bStock rows get
    tradable=False, reason "unlisted". unlisted=False (API list unavailable): status UNKNOWN, tradable as usual."""
    out: list[dict[str, Any]] = []
    for a in data or []:
        tt = LIST_TYPES.get(a.get("type"))  # type: ignore[arg-type]
        sym, addr = a.get("symbol"), a.get("contractAddress")
        if tt is None or not sym or not addr or str(a.get("chainId")) != chain_id:
            continue
        try:
            dec = int(a.get("d") or 18)
        except (TypeError, ValueError):
            dec = 18
        ticker = a.get("ticker")
        row = _row(symbol=sym, name=f"{ticker or sym} ({TYPE_LABELS[tt]})", token_type=tt, chain_id=chain_id,
                   contract=str(addr).lower(), decimals=dec, underlying=ticker, source="public_list")
        row["shares_multiplier"] = _f(a.get("multiplier"))
        if tt == TokenType.XSTOCK or not unlisted:
            out.append(_finish(row, TradingStatus.UNKNOWN, enable_xstock))
        else:  # not in the Web3 API list: we can't see its status, so it is never offered for trading
            row["reason"] = UNLISTED_REASON
            out.append(row)
    return out


def rows_from_tokens(tokens: Iterable[TokenInfo], source: str = "registry",
                     enable_xstock: bool = False, unlisted: bool = False) -> list[dict[str, Any]]:
    """Registry / SIM TokenInfo -> rows (status UNKNOWN unless set later; see rows_from_public_list for unlisted)."""
    out = []
    for t in tokens:
        row = _row(symbol=t.symbol, name=t.name, token_type=t.token_type, chain_id=t.chain_id or BSC,
                   contract=(t.contract_address or "").lower(), decimals=t.decimals, underlying=t.underlying,
                   source=source)
        row["price"], row["volume_24h"] = t.price, t.volume_24h
        _finish(row, TradingStatus.UNKNOWN, enable_xstock)
        if unlisted and row["tradable"]:
            row["tradable"], row["reason"] = False, UNLISTED_REASON
        out.append(row)
    return out


def apply_price_info(rows: list[dict[str, Any]], data: Iterable[Mapping[str, Any]]) -> None:
    """Merge `/market/price-info` rows: volume_24h = DEX buy + sell volume (USD), liquidity, price if missing."""
    by_addr = {r["contract_address"]: r for r in rows}
    for p in data or []:
        r = by_addr.get(str(p.get("tokenContractAddress") or "").lower())
        if r is None:
            continue
        buy, sell = _f(p.get("buyVolume24H")), _f(p.get("sellVolume24H"))
        if buy is not None or sell is not None:
            r["volume_24h"] = round((buy or 0.0) + (sell or 0.0), 2)
        liq = _f(p.get("liquidity"))
        if liq is not None:
            r["liquidity"] = liq
        if r["price"] is None:
            r["price"] = _f(p.get("price"))


def merge_rows(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence of a symbol wins (pass the richest source first). Sorted: type, then symbol."""
    seen: dict[str, dict[str, Any]] = {}
    for g in groups:
        for r in g:
            seen.setdefault(r["symbol"], r)
    order = {"bStock": 0, "Ondo": 1, "xStock": 2}
    return sorted(seen.values(), key=lambda r: (order.get(r["type"], 9), r["symbol"]))


def token_info_from_row(row: Mapping[str, Any]) -> TokenInfo:
    return TokenInfo(symbol=row["symbol"], name=row["name"], token_type=TokenType(row["token_type"]),
                     chain=row["chain"], chain_id=row["chain_id"], contract_address=row["contract_address"],
                     underlying=row.get("underlying"), decimals=int(row.get("decimals") or 18),
                     price=row.get("price"), volume_24h=row.get("volume_24h"))


def count_by_type(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["type"]] = out.get(r["type"], 0) + 1
    return out


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

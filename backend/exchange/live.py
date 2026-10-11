"""BinanceWeb3Client: ExchangeClient backed by the real Binance Web3 API.

- PAPER: real market data + real quotes, simulated fills at the quoted price (no orders sent).
- LIVE : real execution through a LiveExecutor (Agentic Wallet CLI or Web3 API + own signer). Opt-in only
         (see factory.py); every order is capped by `max_live_notional`.
Docs: https://web3.binance.com/en/dev-docs/products/market-api/introduction,
      https://web3.binance.com/en/dev-docs/products/trading-api/introduction
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from backend.contracts import (
    QUOTE_TTL_SECONDS,
    Candle,
    ExecutionPath,
    ExecutionResult,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Quote,
    Timeframe,
    TokenInfo,
    TradingMode,
    utc_now,
)

from .errors import INVALID_PARAMS, ExchangeError, QuoteUnavailable, Web3ApiError
from .executors import LiveExecutor, RouteContext
from .tokens import (
    CHAIN_NAMES,
    DEFAULT_TOKENS,
    QUOTE_ASSETS,
    WALLET_REQUIRED_TYPES,
    from_base_units,
    infer_token_type,
    to_base_units,
)
from .web3_api import Web3ApiClient

log = logging.getLogger(__name__)


class BinanceWeb3Client:
    def __init__(
        self,
        api: Web3ApiClient,
        mode: TradingMode = TradingMode.PAPER,
        tokens: Optional[Iterable[TokenInfo]] = None,
        wallet_address: Optional[str] = None,
        executor: Optional[LiveExecutor] = None,
        live_enabled: bool = False,
        max_live_notional: float = 25.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if mode == TradingMode.SIM:
            raise ValueError("use SimulatedBinanceClient for SIM")
        if mode == TradingMode.LIVE and not (live_enabled and executor is not None):
            raise ValueError("LIVE requires live_enabled=True and an executor")
        self._mode = mode
        self.api = api
        self.wallet_address = wallet_address
        self.executor = executor
        self.max_live_notional = max_live_notional
        self._clock = clock
        self._tokens: dict[str, TokenInfo] = {t.symbol: t for t in (tokens or DEFAULT_TOKENS)}
        self._routes: dict[str, tuple[Quote, RouteContext]] = {}
        self.executions: list[ExecutionResult] = []

    @property
    def mode(self) -> TradingMode:
        return self._mode

    # ------------------------------------------------------------------ tokens
    def _token(self, symbol: str) -> TokenInfo:
        if symbol in self._tokens:
            return self._tokens[symbol]
        for t in self.search_tokens(symbol):
            if t.symbol == symbol and t.chain_id in QUOTE_ASSETS:
                self._tokens[symbol] = t
                return t
        raise QuoteUnavailable(f"unknown token {symbol!r}")

    def search_tokens(self, query: str) -> list[TokenInfo]:
        """`GET /api/v1/dex/market/rwa/search`; empty query -> default tokens with prices."""
        q = query.strip()
        if not q:
            prices = self._prices(list(self._tokens.values()))
            return [t.model_copy(update={"price": prices.get(t.symbol)}) for t in self._tokens.values()]
        out: list[TokenInfo] = []
        for group in self.api.search_rwa(q):
            for a in group.get("assets") or []:
                sym, chain_id, addr = a.get("tokenSymbol"), str(a.get("binanceChainId") or ""), a.get("tokenContractAddress")
                if not sym or not addr:
                    continue
                known = self._tokens.get(sym)
                out.append(TokenInfo(
                    symbol=sym, name=group.get("companyName") or sym,
                    token_type=infer_token_type(sym, a.get("platformId")),
                    chain=CHAIN_NAMES.get(chain_id, chain_id), chain_id=chain_id, contract_address=addr,
                    underlying=group.get("ticker"),
                    decimals=known.decimals if known and known.chain_id == chain_id else (9 if chain_id == "CT_501" else 18),
                ))
        return out

    # ------------------------------------------------------------------ market data
    def get_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        tok = self._token(symbol)
        rows = self.api.get_candles(tok.chain_id or "", tok.contract_address or "", bar=timeframe.value, limit=limit)
        candles: list[Candle] = []
        for r in rows:  # [open, high, low, close, volume, timestamp_ms, tradeCount]
            try:
                o, h, l, c, v, ts = (float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(float(r[5])))
                candles.append(Candle(timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                                      open=o, high=max(h, o, c), low=min(l, o, c), close=c, volume=max(v, 0.0)))
            except (IndexError, TypeError, ValueError):
                log.warning("skipping malformed candle row for %s: %r", symbol, r)
        candles.sort(key=lambda k: k.timestamp)
        return candles[-limit:]

    def _prices(self, toks: list[TokenInfo]) -> dict[str, float]:
        out: dict[str, float] = {}
        by_chain: dict[str, list[TokenInfo]] = {}
        for t in toks:
            by_chain.setdefault(t.chain_id or "", []).append(t)
        for chain_id, group in by_chain.items():
            addr_to_sym = {(t.contract_address or "").lower(): t.symbol for t in group}
            for row in self.api.get_rwa_price(chain_id, [t.contract_address or "" for t in group]):
                sym = addr_to_sym.get(str(row.get("tokenContractAddress", "")).lower())
                px = row.get("tokenPrice") or row.get("referencePrice")
                if sym and px not in (None, ""):
                    out[sym] = float(px)
        return out

    def get_price(self, symbol: str) -> float:
        tok = self._token(symbol)
        px = self._prices([tok]).get(symbol)
        if px is None or px <= 0:
            candles = self.get_candles(symbol, Timeframe.M1, limit=1)
            if not candles:
                raise QuoteUnavailable(f"no price for {symbol}")
            px = candles[-1].close
        return px

    # ------------------------------------------------------------------ quotes
    def get_quote(self, symbol: str, side: OrderSide, notional: float) -> Quote:
        """`GET /api/v1/dex/aggregator/quote` (best route). Raises QuoteUnavailable on API errors such as
        40367 market closed / 40375 below 20 USD minimum / 40374 no liquidity.

        xStock quotes are RFQ and need `userWalletAddress` (API: 40001 "userWalletAddress is required for RFQ (xStock)
        quote"), so without a wallet we fail fast with that code instead of spending a request."""
        tok = self._token(symbol)
        if tok.token_type in WALLET_REQUIRED_TYPES and not self.wallet_address:
            raise QuoteUnavailable(f"{INVALID_PARAMS} userWalletAddress is required for RFQ ({tok.token_type.value}) "
                                   f"quote: set BINANCE_WALLET_ADDRESS", INVALID_PARAMS)
        stable = QUOTE_ASSETS.get(tok.chain_id or "")
        if stable is None:
            raise QuoteUnavailable(f"no quote asset configured for chain {tok.chain_id}")
        if side == OrderSide.BUY:
            from_addr, to_addr, from_dec, to_dec, from_qty = stable.address, tok.contract_address, stable.decimals, tok.decimals, notional
        else:
            from_qty = notional / self.get_price(symbol)
            from_addr, to_addr, from_dec, to_dec = tok.contract_address, stable.address, tok.decimals, stable.decimals
        amount = to_base_units(from_qty, from_dec)
        try:
            routes = self.api.get_quote(tok.chain_id or "", from_addr or "", to_addr or "", amount,
                                        user_wallet_address=self.wallet_address)
        except Web3ApiError as e:
            raise QuoteUnavailable(str(e), e.code) from e
        if not routes:
            raise QuoteUnavailable(f"no route for {symbol}")
        route = next((r for r in routes if r.get("isBest")), routes[0])
        to_qty = from_base_units(route.get("toTokenAmount"), to_dec)
        if to_qty <= 0:
            raise QuoteUnavailable(f"zero output quote for {symbol}")
        qty, usd = (to_qty, notional) if side == OrderSide.BUY else (from_qty, to_qty)
        mode = str(route.get("executionMode") or "SWAP").upper()
        now = self._clock()
        quote = Quote(
            symbol=symbol, side=side, path=ExecutionPath.RFQ if mode == "RFQ" else ExecutionPath.SWAP,
            notional=round(usd, 8), quantity=qty, price=usd / qty,
            fee=float(route.get("tradeFee") or 0.0), created_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        )
        ctx = RouteContext(chain_id=tok.chain_id or "", from_token=from_addr or "", to_token=to_addr or "",
                           from_decimals=from_dec, to_decimals=to_dec, amount=amount, from_qty=from_qty,
                           binance_quote_id=route.get("quoteId"), vendor=route.get("vendorName"),
                           execution_mode=mode, raw_route=route)
        self._routes[quote.quote_id] = (quote, ctx)
        return quote

    # ------------------------------------------------------------------ execution
    def execute_swap(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        return self._execute(order, quote, ExecutionPath.SWAP)

    def execute_rfq(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        return self._execute(order, quote, ExecutionPath.RFQ)

    def _execute(self, order: OrderRequest, quote: Quote, path: ExecutionPath) -> ExecutionResult:
        now = self._clock()
        base = dict(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side, path=path,
                    mode=self._mode, timestamp=now)

        def done(res: ExecutionResult) -> ExecutionResult:
            self.executions.append(res)
            return res

        def reject(msg: str, status: OrderStatus = OrderStatus.REJECTED) -> ExecutionResult:
            return done(ExecutionResult(status=status, error=msg, **base))

        stored = self._routes.get(quote.quote_id)
        if stored is None or quote.symbol != order.symbol or quote.side != order.side:
            return reject("unknown or mismatched quote")
        if order.mode == TradingMode.LIVE and self._mode != TradingMode.LIVE:
            return reject(f"LIVE order sent to a {self._mode.value} client")
        if quote.is_expired(now):
            self._routes.pop(quote.quote_id, None)
            return reject("quote expired", OrderStatus.EXPIRED)
        if quote.path != path:
            return reject(f"quote is a {quote.path.value} route; call execute_{quote.path.value.lower()}()")
        _, ctx = stored
        if self._mode == TradingMode.PAPER:
            qty = order.quantity if (order.side == OrderSide.SELL and order.quantity) else quote.quantity
            self._routes.pop(quote.quote_id, None)
            return done(ExecutionResult(status=OrderStatus.FILLED, filled_quantity=qty, avg_price=quote.price,
                                        notional=round(qty * quote.price, 8), fee=quote.fee, **base))
        # LIVE
        if quote.notional > self.max_live_notional:
            return reject(f"notional {quote.notional:.2f} exceeds BINANCE_LIVE_MAX_NOTIONAL={self.max_live_notional:.2f}")
        if order.mode != TradingMode.LIVE:
            return reject("LIVE client requires OrderRequest.mode == LIVE (explicit opt-in per order)")
        self._routes.pop(quote.quote_id, None)
        try:
            assert self.executor is not None
            return done(self.executor.execute(order, quote, ctx, path))
        except ExchangeError as e:
            return reject(str(e), OrderStatus.FAILED)

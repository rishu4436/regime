"""BinanceWeb3Client: ExchangeClient backed by the real Binance Web3 API.

- PAPER: real market data + real quotes, simulated fills at the quoted price (no orders sent).
- LIVE : real execution through a LiveExecutor (Agentic Wallet CLI or Web3 API + own signer). Opt-in only
         (see factory.py); every order is capped by `max_live_notional`.
Docs: https://web3.binance.com/en/dev-docs/products/market-api/introduction,
      https://web3.binance.com/en/dev-docs/products/trading-api/introduction
"""
from __future__ import annotations

import logging
import time
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
from .status import (
    QuoteReason,
    TokenStatus,
    TokenStatusMixin,
    TradingStatus,
    apply_status_info,
    default_min_order_usdt,
    finalize_status,
    is_xstock,
    parse_rwa_dynamic,
    reason_for_error_code,
    status_from_candles,
    xstock_enabled,
)
from .web3_api import PublicRwaClient, Web3ApiClient

log = logging.getLogger(__name__)

CANDLE_PAGE_SIZE = 300   # API max per request (limit=301 -> 40001 "invalid limit range")
CANDLE_MAX_PAGES = 20    # hard cap per call (~6000 bars); one request per page


class BinanceWeb3Client(TokenStatusMixin):
    # GET /market/candles: max `limit` is 300 (verified live 2026-10-11); 5 RPS per endpoint.
    candle_page_size: int = CANDLE_PAGE_SIZE
    candle_max_pages: int = CANDLE_MAX_PAGES
    candle_page_pause_s: float = 0.25

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
        public: Optional[PublicRwaClient] = None,
        enable_xstock: Optional[bool] = None,
        status_ttl_s: float = 60.0,
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
        # Public (no-key) RWA status endpoints; None disables them (status then comes from rwa/price + candles).
        self.public = public
        self.enable_xstock = xstock_enabled() if enable_xstock is None else enable_xstock
        self._status_ttl = timedelta(seconds=status_ttl_s)
        self._status_cache: dict[str, TokenStatus] = {}
        self._quote_meta = {}
        self._candle_sleep: Callable[[float], None] = time.sleep

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
        """Newest `limit` candles, oldest first. Pages `GET /market/candles` when `limit` > CANDLE_PAGE_SIZE.

        Verified live (NVDAB, 2026-10-11): `limit` 300 is the maximum (301 -> 40001 "invalid limit range");
        `after=<ts>` returns the bars at or before ts (inclusive, i.e. older), `before=<ts>` the newer ones.
        So we page backwards with `after=<oldest ts seen>`, dedupe the overlapping bar, and stop on a short or
        empty page, on a page that adds nothing new, once `limit` is reached, or after CANDLE_MAX_PAGES."""
        tok = self._token(symbol)
        limit = max(int(limit), 0)
        if limit == 0:
            return []
        page_size = max(2, int(self.candle_page_size))
        by_ts: dict[int, Candle] = {}
        after: Optional[int] = None
        for page in range(int(self.candle_max_pages)):
            # `after` is inclusive, so later pages ask for one extra bar (the overlap is deduped below).
            want = min(page_size, limit - len(by_ts) + (0 if after is None else 1))
            if page:
                self._candle_sleep(self.candle_page_pause_s)  # stay under 5 RPS per endpoint
            try:
                rows = self.api.get_candles(tok.chain_id or "", tok.contract_address or "", bar=timeframe.value,
                                            limit=want, after=after)
            except Web3ApiError:
                if not by_ts:
                    raise
                log.warning("candle page %d for %s %s failed; returning %d bars", page + 1, symbol,
                            timeframe.value, len(by_ts), exc_info=True)
                break
            rows = rows or []
            added = 0
            for c in self._parse_candles(symbol, rows):
                key = int(c.timestamp.timestamp() * 1000)
                if key not in by_ts:
                    by_ts[key] = c
                    added += 1
            if len(by_ts) >= limit or not rows or added == 0 or len(rows) < want:
                break
            after = min(by_ts)
        candles = sorted(by_ts.values(), key=lambda k: k.timestamp)
        return candles[-limit:]

    @staticmethod
    def _parse_candles(symbol: str, rows: Iterable) -> list[Candle]:
        candles: list[Candle] = []
        for r in rows:  # [open, high, low, close, volume, timestamp_ms, tradeCount]
            try:
                o, h, l, c, v, ts = (float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(float(r[5])))
                candles.append(Candle(timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                                      open=o, high=max(h, o, c), low=min(l, o, c), close=c, volume=max(v, 0.0)))
            except (IndexError, TypeError, ValueError):
                log.warning("skipping malformed candle row for %s: %r", symbol, r)
        return candles

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
        if is_xstock(symbol, tok.token_type) and not self.enable_xstock:
            raise QuoteUnavailable(f"{symbol}: xStock disabled (zero BSC liquidity); set BINANCE_ENABLE_XSTOCK=true",
                                   reason=QuoteReason.XSTOCK_DISABLED.value)
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
            raise QuoteUnavailable(str(e), e.code, reason=reason_for_error_code(e.code)) from e
        if not routes:
            raise QuoteUnavailable(f"no route for {symbol}", reason=QuoteReason.INSUFFICIENT_LIQUIDITY.value)
        route = next((r for r in routes if r.get("isBest")), routes[0])
        to_qty = from_base_units(route.get("toTokenAmount"), to_dec)
        if to_qty <= 0:
            raise QuoteUnavailable(f"zero output quote for {symbol}", reason=QuoteReason.INSUFFICIENT_LIQUIDITY.value)
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
        # Extras for risk/UI (quote_meta). No extra requests here: use the cached status (quote_or_reason and
        # get_token_status warm it); price impact comes from the route's priceImpactPercent.
        st = self._status_cache.get(symbol) or finalize_status(
            TokenStatus(symbol, tok.token_type.value, TradingStatus.UNKNOWN, True,
                        default_min_order_usdt(tok.token_type), checked_at=now), self.enable_xstock)
        self._record_quote_meta(quote, st, st.price, route)
        return quote

    def _raw_route(self, quote_id: str):
        stored = self._routes.get(quote_id)
        return stored[1].raw_route if stored else None

    # ------------------------------------------------------------------ token status
    def get_token_status(self, symbol: str, refresh: bool = False) -> TokenStatus:
        """Trading status, min order, shares multiplier and reference price for `symbol` (cached status_ttl_s).

        Sources, best first: public `rwa/dynamic/ai` (statusInfo + sharesMultiplier + stockInfo.price),
        HMAC `rwa/price` (tokenPrice/referencePrice), then 1h candles (staleness / zero volume) when no statusInfo.
        xStock is short-circuited to NO_LIQUIDITY/xstock_disabled unless enabled (no network calls)."""
        now = self._clock()
        cached = self._status_cache.get(symbol)
        if cached and not refresh and now - cached.checked_at < self._status_ttl:
            return cached
        tok = self._token(symbol)
        st = TokenStatus(symbol=symbol, token_type=tok.token_type.value, trading_status=TradingStatus.UNKNOWN,
                         tradable=True, min_order_usdt=default_min_order_usdt(tok.token_type), checked_at=now)
        if is_xstock(symbol, tok.token_type) and not self.enable_xstock:
            st.trading_status, st.source = TradingStatus.NO_LIQUIDITY, "policy"
            return self._cache_status(finalize_status(st, False))
        info = None
        if self.public is not None:
            try:
                dyn = parse_rwa_dynamic(self.public.rwa_dynamic(tok.chain_id or "", tok.contract_address or ""))
                st.price, st.shares_multiplier, st.reference_price = dyn["price"], dyn["shares_multiplier"], dyn["reference_price"]
                st.max_order_usdt = dyn["max_notional_usdt"]
                info = dyn["status_info"]
                apply_status_info(st, info)
                st.source = "rwa_dynamic"
            except Exception as e:  # noqa: BLE001 - status is best-effort
                log.warning("rwa dynamic failed for %s: %s", symbol, e)
        try:
            for row in self.api.get_rwa_price(tok.chain_id or "", [tok.contract_address or ""]):
                if str(row.get("tokenContractAddress", "")).lower() == (tok.contract_address or "").lower():
                    st.price = float(row["tokenPrice"]) if row.get("tokenPrice") not in (None, "") else st.price
                    if st.reference_price is None and row.get("referencePrice") not in (None, ""):
                        st.reference_price = float(row["referencePrice"])
        except ExchangeError as e:
            log.warning("rwa price failed for %s: %s", symbol, e)
        if not info:
            try:
                st.trading_status = status_from_candles(self.get_candles(symbol, Timeframe.H1, limit=24), now)
                st.source = "candles"
            except ExchangeError as e:
                log.warning("candles failed for %s: %s", symbol, e)
        if st.reference_price is None and st.price and st.shares_multiplier:
            st.reference_price = st.price / st.shares_multiplier
        return self._cache_status(finalize_status(st, self.enable_xstock))

    def _cache_status(self, st: TokenStatus) -> TokenStatus:
        self._status_cache[st.symbol] = st
        return st

    # ------------------------------------------------------------------ execution
    def execute_swap(self, order: OrderRequest, quote: Quote,
                     max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        """`max_slippage_pct` (percent, e.g. risk's cap) overrides the executor default for this LIVE order."""
        return self._execute(order, quote, ExecutionPath.SWAP, max_slippage_pct)

    def execute_rfq(self, order: OrderRequest, quote: Quote,
                    max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        return self._execute(order, quote, ExecutionPath.RFQ, max_slippage_pct)

    def _execute(self, order: OrderRequest, quote: Quote, path: ExecutionPath,
                 max_slippage_pct: Optional[float] = None) -> ExecutionResult:
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
            import inspect  # never retry on TypeError: a second call could submit a second swap
            if max_slippage_pct is not None and "max_slippage_pct" in inspect.signature(self.executor.execute).parameters:
                return done(self.executor.execute(order, quote, ctx, path, max_slippage_pct=max_slippage_pct))
            return done(self.executor.execute(order, quote, ctx, path))
        except ExchangeError as e:
            return reject(str(e), OrderStatus.FAILED)

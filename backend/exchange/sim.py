"""SimulatedBinanceClient: offline ExchangeClient that mimics the Binance Web3 Trading API.

Market data comes from the coordinator's deterministic mocks (so every bot sees the same candles/prices), while
the execution rules follow the real API (https://web3.binance.com/en/dev-docs/products/trading-api/introduction):
- Routing (`executionMode`): Ondo (`...on`) and xStock (`...x`) quote as RFQ, BStock (`...B`) as SWAP.
  Verified with read-only PAPER quotes on 2026-10-11: an xStock quote without `userWalletAddress` fails with
  40001 "userWalletAddress is required for RFQ (xStock) quote"; a 25 USDT -> NVDAB quote came back as a SWAP route.
  (PAPER/LIVE always follow the route's real `executionMode`; Ondo can also come back as a DEX SWAP route.)
- Quote TTL 30 s; an expired quote returns EXPIRED (real API: QUOTE_EXPIRED 40401).
- Executing a quote on the wrong path is REJECTED (real API: RFQ routes have no `tx`, SWAP routes have no `rfq`).
- Ondo minimum order 20 USD (40375 ONDO_FROM_USD_AMOUNT_TOO_SMALL) -> REJECTED.
- Optional US-market-hours gate for tokenized stocks (Ondo 40367, BStock 40369, xStock RFQ 40374 - the code a
  weekend xStock quote returned) and simulated latency, both off by default.
- Replay: `set_replay_index(i)` pins the "current" bar to index i of the fixed n_candles-bar series, so
  candles/prices/quotes/fills behave as if time stood at bar i (for bar-by-bar SIM loops).
Never touches the network and refuses LIVE.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

from backend.contracts import (
    QUOTE_TTL_SECONDS,
    Candle,
    ExecutionPath,
    ExecutionResult,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Quote,
    Regime,
    Timeframe,
    TokenInfo,
    TokenType,
    TradingMode,
    utc_now,
)
from backend.mocks import MockExchangeClient, generate_candles
from backend.mocks.exchange import FEE_RATE, RFQ_FEE_RATE

from .errors import (
    QuoteUnavailable,
    BSTOCK_INVALID_TRADING_TIME,
    ONDO_FROM_USD_AMOUNT_TOO_SMALL,
    ONDO_MARKET_STATE_NOT_TRADABLE,
    RWA_INSUFFICIENT_LIQUIDITY,
)
from .status import (
    QuoteReason,
    TokenStatus,
    TokenStatusMixin,
    TradingStatus,
    default_min_order_usdt,
    finalize_status,
    is_xstock,
    xstock_enabled,
)
from .tokens import DEFAULT_TOKENS
from .wallet import SimWalletMixin

ONDO_MIN_ORDER_USD = 20.0
# xStock quotes are RFQ (need userWalletAddress, else 40001); BStock supports SWAP (observed) as well as RFQ.
RFQ_TYPES = {TokenType.ONDO, TokenType.XSTOCK}
MARKET_HOURS_TYPES = {TokenType.ONDO, TokenType.BSTOCK, TokenType.XSTOCK}
MARKET_CLOSED_CODE = {
    TokenType.ONDO: ONDO_MARKET_STATE_NOT_TRADABLE,
    TokenType.BSTOCK: BSTOCK_INVALID_TRADING_TIME,
    TokenType.XSTOCK: RWA_INSUFFICIENT_LIQUIDITY,
}


def is_us_market_open(now: datetime) -> bool:
    """Team decision (KICKOFF): market hours = weekdays 13:00-20:00 UTC."""
    return now.weekday() < 5 and 13 <= now.hour < 20


def is_ondo_offhours(now: datetime) -> bool:
    """Ondo regular/overnight sessions run 24/5; the weekend is the reference-price-only "offhours" session
    (observed Sun 2026-10-11: marketStatus=offhours, nextOpenTime Mon 00:05 UTC). SIM: Sat/Sun UTC = OFFHOURS."""
    return now.weekday() >= 5


# SIM shares multipliers (real values for the NVDA tokens from rwa_dynamic samples; 1.0 otherwise).
SIM_SHARES_MULTIPLIER = {"NVDAon": 1.0017152487959898, "NVDAB": 1.000778223752807865}


def path_for(token_type: TokenType) -> ExecutionPath:
    return ExecutionPath.RFQ if token_type in RFQ_TYPES else ExecutionPath.SWAP


class SimulatedBinanceClient(SimWalletMixin, TokenStatusMixin, MockExchangeClient):
    def __init__(
        self,
        regimes: Optional[dict[str, Regime]] = None,
        seed: int = 42,
        n_candles: int = 250,
        mode: TradingMode = TradingMode.SIM,
        clock: Callable[[], datetime] = utc_now,
        enforce_market_hours: bool = False,
        enforce_paths: bool = True,
        latency_s: float = 0.0,
        sleep: Optional[Callable[[float], None]] = None,
        enable_xstock: Optional[bool] = None,
    ) -> None:
        if mode != TradingMode.SIM:
            raise ValueError("SimulatedBinanceClient only runs in SIM mode")
        super().__init__(regimes=regimes, seed=seed, n_candles=n_candles, mode=mode, clock=clock)
        # Same symbols/start prices as the mocks, enriched with the real chain ids / contract addresses.
        real = {t.symbol: t for t in DEFAULT_TOKENS}
        self._tokens = {
            s: t.model_copy(update={k: getattr(real[s], k) for k in ("chain", "chain_id", "contract_address", "decimals")})
            if s in real else t
            for s, t in self._tokens.items()
        }
        self.enforce_market_hours = enforce_market_hours
        self.enforce_paths = enforce_paths
        self._latency = latency_s
        self._sleep = sleep
        self._replay_index: Optional[int] = None
        self._series_cache: dict[tuple, list[Candle]] = {}
        self.enable_xstock = xstock_enabled() if enable_xstock is None else enable_xstock
        self._quote_meta = {}
        self._status_override: dict[str, TradingStatus] = {}

    # ------------------------------------------------------------------ token status
    def set_trading_status(self, symbol: str, status: Optional[TradingStatus]) -> None:
        """Force a status for scenarios/tests (None clears)."""
        if status is None:
            self._status_override.pop(symbol, None)
        else:
            self._status_override[symbol] = status

    def get_token_status(self, symbol: str) -> TokenStatus:
        """SIM emulation of the real statuses: bStock TRADING 24/7; Ondo OFFHOURS on weekends (TRADING otherwise);
        xStock NO_LIQUIDITY (+ xstock_disabled) unless enabled; enforce_market_hours=True -> CLOSED outside
        13:00-20:00 UTC weekdays. Ondo min order 20 USDT, others 10."""
        tok = self._token(symbol)
        now = self._clock()
        if symbol in self._status_override:
            status = self._status_override[symbol]
        elif is_xstock(symbol, tok.token_type):
            status = TradingStatus.TRADING if self.enable_xstock else TradingStatus.NO_LIQUIDITY
        elif self.enforce_market_hours and tok.token_type in MARKET_HOURS_TYPES and not is_us_market_open(now):
            status = TradingStatus.CLOSED
        elif tok.token_type == TokenType.ONDO and is_ondo_offhours(now):
            status = TradingStatus.OFFHOURS
        else:
            status = TradingStatus.TRADING
        price = self.get_price(symbol)
        mult = SIM_SHARES_MULTIPLIER.get(symbol, 1.0)
        st = TokenStatus(symbol=symbol, token_type=tok.token_type.value, trading_status=status, tradable=True,
                         min_order_usdt=default_min_order_usdt(tok.token_type), shares_multiplier=mult,
                         reference_price=price / mult, price=price,
                         market_status="offhours" if status == TradingStatus.OFFHOURS else None,
                         source="sim", checked_at=now)
        return finalize_status(st, self.enable_xstock)

    # ------------------------------------------------------------------ replay
    @property
    def replay_index(self) -> Optional[int]:
        """Requested replay bar index (None = live/full-series behaviour). Clamped per symbol when used."""
        return self._replay_index

    def set_replay_index(self, i: Optional[int]) -> None:
        """Pin the current bar to index `i` (0 = oldest) of each symbol's `n_bars(symbol)`-bar series.

        - get_candles(symbol, tf, limit) -> bars[0..i] (inclusive), then the last `limit` of that slice.
        - get_price(symbol) -> bars[i].close (a set_price() override still wins), so get_quote and fills use it.
        - Out-of-range i is clamped to [0, n_bars-1]; None restores the default full-series behaviour.
        """
        self._replay_index = None if i is None else int(i)

    def n_bars(self, symbol: str) -> int:
        """Length of the replayable series for `symbol` (the client's n_candles; same for every symbol)."""
        self._token(symbol)
        return self._n

    def _series(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        tok = self._token(symbol)
        regime = self._regimes.get(symbol, Regime.SIDEWAYS)
        key = (symbol, timeframe, regime, tok.price)
        if key not in self._series_cache:
            self._series_cache[key] = generate_candles(regime, n=self._n, seed=self._seed,
                                                       start_price=tok.price or 100.0, timeframe=timeframe)
        return self._series_cache[key]

    def _clamped_index(self, symbol: str) -> int:
        assert self._replay_index is not None
        return max(0, min(self._replay_index, self.n_bars(symbol) - 1))

    def get_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        if self._replay_index is None:
            return super().get_candles(symbol, timeframe, limit)
        upto = self._series(symbol, timeframe)[: self._clamped_index(symbol) + 1]
        return upto[-limit:] if limit > 0 else []

    def get_price(self, symbol: str) -> float:
        if self._replay_index is None or symbol in self._price_override:
            return super().get_price(symbol)
        return self._series(symbol, Timeframe.H1)[self._clamped_index(symbol)].close

    def _maybe_sleep(self) -> None:
        if self._latency > 0:
            import time

            (self._sleep or time.sleep)(self._latency)

    def add_token(self, token: TokenInfo) -> None:
        """Register an extra token for scenarios (price must be set)."""
        self._tokens[token.symbol] = token

    def get_quote(self, symbol: str, side: OrderSide, notional: float, path: Optional[ExecutionPath] = None) -> Quote:
        tok = self._token(symbol)
        if is_xstock(symbol, tok.token_type) and not self.enable_xstock:
            raise QuoteUnavailable(f"{symbol}: xStock disabled (zero BSC liquidity); set BINANCE_ENABLE_XSTOCK=true",
                                   reason=QuoteReason.XSTOCK_DISABLED.value)
        self._maybe_sleep()
        route = path or path_for(tok.token_type)
        price = self.get_price(symbol)
        fee_rate = RFQ_FEE_RATE if route == ExecutionPath.RFQ else FEE_RATE
        now = self._clock()
        if side == OrderSide.BUY:
            qty = notional * (1 - fee_rate) / price
        else:
            qty = notional / price
        q = Quote(
            symbol=symbol, side=side, path=route, notional=notional, price=price, quantity=round(qty, 8),
            fee=round(notional * fee_rate, 8), created_at=now, expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        )
        self._quotes[q.quote_id] = q
        self._record_quote_meta(q, self.get_token_status(symbol), price)
        return q

    def _venue_check(self, order: OrderRequest, quote: Quote, path: ExecutionPath) -> Optional[ExecutionResult]:
        tok = self._token(order.symbol)
        now = self._clock()
        base = dict(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side, path=path,
                    mode=self._mode, timestamp=now)

        def reject(msg: str) -> ExecutionResult:
            res = ExecutionResult(status=OrderStatus.REJECTED, error=msg, **base)
            self.executions.append(res)
            return res

        if self.enforce_paths and quote.path != path:
            return reject(f"quote is a {quote.path.value} route; call execute_{quote.path.value.lower()}()")
        if tok.token_type == TokenType.ONDO and order.side == OrderSide.BUY and quote.notional < ONDO_MIN_ORDER_USD:
            return reject(f"{ONDO_FROM_USD_AMOUNT_TOO_SMALL} ONDO_FROM_USD_AMOUNT_TOO_SMALL: "
                          f"Minimum order amount is {ONDO_MIN_ORDER_USD:.0f} USD")
        if self.enforce_market_hours and tok.token_type in MARKET_HOURS_TYPES and not is_us_market_open(now):
            code = MARKET_CLOSED_CODE[tok.token_type]
            return reject(f"{code} market closed for {order.symbol}")
        return None

    def execute_swap(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        self._maybe_sleep()
        return self._venue_check(order, quote, ExecutionPath.SWAP) or self._finish(order, quote, ExecutionPath.SWAP)

    def execute_rfq(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        self._maybe_sleep()
        return self._venue_check(order, quote, ExecutionPath.RFQ) or self._finish(order, quote, ExecutionPath.RFQ)

    def _finish(self, order: OrderRequest, quote: Quote, path: ExecutionPath) -> ExecutionResult:
        res = self._execute(order, quote, path)
        if res.status == OrderStatus.FILLED and res.tx_hash:
            res = res.model_copy(update={"tx_hash": res.tx_hash.replace("0xmock", "0xsim", 1)})
            self.executions[-1] = res
        return res

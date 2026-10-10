"""SimulatedBinanceClient: offline ExchangeClient that mimics the Binance Web3 Trading API.

Market data comes from the coordinator's deterministic mocks (so every bot sees the same candles/prices), while
the execution rules follow the real API (https://web3.binance.com/en/dev-docs/products/trading-api/introduction):
- Routing: Ondo (`...on`) and BStock (`...B`) quote as RFQ, xStock (`...x`) as SWAP (`executionMode`).
- Quote TTL 30 s; an expired quote returns EXPIRED (real API: QUOTE_EXPIRED 40401).
- Executing a quote on the wrong path is REJECTED (real API: RFQ routes have no `tx`, SWAP routes have no `rfq`).
- Ondo minimum order 20 USD (40375 ONDO_FROM_USD_AMOUNT_TOO_SMALL) -> REJECTED.
- Optional US-market-hours gate (40367 / 40369) and simulated latency, both off by default.
Never touches the network and refuses LIVE.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

from backend.contracts import (
    QUOTE_TTL_SECONDS,
    ExecutionPath,
    ExecutionResult,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Quote,
    Regime,
    TokenInfo,
    TokenType,
    TradingMode,
    utc_now,
)
from backend.mocks import MockExchangeClient
from backend.mocks.exchange import FEE_RATE, RFQ_FEE_RATE

from .errors import BSTOCK_INVALID_TRADING_TIME, ONDO_FROM_USD_AMOUNT_TOO_SMALL, ONDO_MARKET_STATE_NOT_TRADABLE
from .tokens import DEFAULT_TOKENS

ONDO_MIN_ORDER_USD = 20.0
RFQ_TYPES = {TokenType.ONDO, TokenType.BSTOCK}


def is_us_market_open(now: datetime) -> bool:
    """Team decision (KICKOFF): market hours = weekdays 13:00-20:00 UTC."""
    return now.weekday() < 5 and 13 <= now.hour < 20


def path_for(token_type: TokenType) -> ExecutionPath:
    return ExecutionPath.RFQ if token_type in RFQ_TYPES else ExecutionPath.SWAP


class SimulatedBinanceClient(MockExchangeClient):
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

    def _maybe_sleep(self) -> None:
        if self._latency > 0:
            import time

            (self._sleep or time.sleep)(self._latency)

    def add_token(self, token: TokenInfo) -> None:
        """Register an extra token for scenarios (price must be set)."""
        self._tokens[token.symbol] = token

    def get_quote(self, symbol: str, side: OrderSide, notional: float, path: Optional[ExecutionPath] = None) -> Quote:
        tok = self._token(symbol)
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
        if self.enforce_market_hours and tok.token_type in RFQ_TYPES and not is_us_market_open(now):
            code = ONDO_MARKET_STATE_NOT_TRADABLE if tok.token_type == TokenType.ONDO else BSTOCK_INVALID_TRADING_TIME
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

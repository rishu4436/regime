"""MockExchangeClient: a deterministic, offline implementation of the ExchangeClient Protocol."""
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

from .candles import generate_candles

# Start prices match the reference simulator (NVDAon 140, TSLAx 200, AAPLB 170).
MOCK_TOKENS: list[TokenInfo] = [
    TokenInfo(symbol="NVDAon", name="NVIDIA (Ondo tokenized)", token_type=TokenType.ONDO, chain="BSC", underlying="NVDA", price=140.0),
    TokenInfo(symbol="TSLAx", name="Tesla xStock", token_type=TokenType.XSTOCK, chain="BSC", underlying="TSLA", price=200.0),
    TokenInfo(symbol="AAPLB", name="Apple BStock", token_type=TokenType.BSTOCK, chain="BSC", underlying="AAPL", price=170.0),
    TokenInfo(symbol="SPYon", name="SPDR S&P 500 ETF (Ondo tokenized)", token_type=TokenType.ONDO, chain="Ethereum", underlying="SPY", price=560.0),
    TokenInfo(symbol="METAx", name="Meta xStock", token_type=TokenType.XSTOCK, chain="Solana", underlying="META", price=590.0),
]

DEFAULT_REGIMES: dict[str, Regime] = {
    "NVDAon": Regime.STRONG_UPTREND,
    "TSLAx": Regime.HIGH_VOLATILITY,
    "AAPLB": Regime.SIDEWAYS,
    "SPYon": Regime.MILD_UPTREND,
    "METAx": Regime.OVERSOLD_PULLBACK,
}

FEE_RATE = 0.001  # 0.1%
RFQ_FEE_RATE = 0.0005


class MockExchangeClient:
    """Offline exchange. Never touches the network. Refuses LIVE mode.

    - Candles are deterministic per (symbol, regime, seed); price = last close (or override via set_price).
    - Quotes expire after QUOTE_TTL_SECONDS; executing an expired quote returns status EXPIRED.
    - Inject `clock` in tests to control time.
    """

    def __init__(
        self,
        regimes: Optional[dict[str, Regime]] = None,
        seed: int = 42,
        n_candles: int = 250,
        mode: TradingMode = TradingMode.SIM,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if mode == TradingMode.LIVE:
            raise ValueError("MockExchangeClient cannot run in LIVE mode")
        self._mode = mode
        self._regimes = {**DEFAULT_REGIMES, **(regimes or {})}
        self._seed = seed
        self._n = n_candles
        self._clock = clock
        self._tokens = {t.symbol: t for t in MOCK_TOKENS}
        self._price_override: dict[str, float] = {}
        self._quotes: dict[str, Quote] = {}
        self.executions: list[ExecutionResult] = []

    @property
    def mode(self) -> TradingMode:
        return self._mode

    def _token(self, symbol: str) -> TokenInfo:
        if symbol not in self._tokens:
            raise KeyError(f"unknown token {symbol!r}")
        return self._tokens[symbol]

    def set_regime(self, symbol: str, regime: Regime) -> None:
        self._regimes[symbol] = regime

    def set_price(self, symbol: str, price: float) -> None:
        self._price_override[symbol] = price

    def get_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        tok = self._token(symbol)
        candles = generate_candles(
            self._regimes.get(symbol, Regime.SIDEWAYS), n=max(limit, self._n), seed=self._seed,
            start_price=tok.price or 100.0, timeframe=timeframe,
        )
        return candles[-limit:]

    def get_price(self, symbol: str) -> float:
        if symbol in self._price_override:
            return self._price_override[symbol]
        return self.get_candles(symbol, limit=self._n)[-1].close

    def search_tokens(self, query: str) -> list[TokenInfo]:
        q = query.strip().lower()
        out = []
        for t in self._tokens.values():
            if not q or q in t.symbol.lower() or q in t.name.lower() or q in (t.underlying or "").lower():
                out.append(t.model_copy(update={"price": self.get_price(t.symbol)}))
        return out

    def get_quote(self, symbol: str, side: OrderSide, notional: float, path: ExecutionPath = ExecutionPath.SWAP) -> Quote:
        price = self.get_price(symbol)
        fee_rate = RFQ_FEE_RATE if path == ExecutionPath.RFQ else FEE_RATE
        now = self._clock()
        q = Quote(
            symbol=symbol, side=side, path=path, notional=notional, price=price,
            quantity=round(notional * (1 - fee_rate) / price, 8), fee=round(notional * fee_rate, 8),
            created_at=now, expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        )
        self._quotes[q.quote_id] = q
        return q

    def _execute(self, order: OrderRequest, quote: Quote, path: ExecutionPath) -> ExecutionResult:
        base = dict(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side, path=path, mode=self._mode)
        now = self._clock()
        if order.mode == TradingMode.LIVE:
            res = ExecutionResult(status=OrderStatus.REJECTED, error="mock exchange refuses LIVE orders", timestamp=now, **base)
        elif quote.quote_id not in self._quotes or quote.symbol != order.symbol or quote.side != order.side:
            res = ExecutionResult(status=OrderStatus.REJECTED, error="unknown or mismatched quote", timestamp=now, **base)
        elif quote.is_expired(now):
            res = ExecutionResult(status=OrderStatus.EXPIRED, error="quote expired", timestamp=now, **base)
        else:
            qty = order.quantity if (order.side == OrderSide.SELL and order.quantity) else quote.quantity
            res = ExecutionResult(
                status=OrderStatus.FILLED, filled_quantity=qty, avg_price=quote.price,
                notional=round(qty * quote.price, 8), fee=quote.fee, tx_hash=f"0xmock{quote.quote_id[:16]}",
                timestamp=now, **base,
            )
            self._quotes.pop(quote.quote_id, None)
        self.executions.append(res)
        return res

    def execute_swap(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        return self._execute(order, quote, ExecutionPath.SWAP)

    def execute_rfq(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        return self._execute(order, quote, ExecutionPath.RFQ)

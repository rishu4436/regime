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
- Replay is time-based: `set_replay_time(t)` (or `set_replay_index(i, timeframe)`, which pins the time of bar i)
  makes candles on every timeframe stop at t and price/quotes/fills use the price at t, so switching timeframe never
  moves the clock. Every timeframe shares one price path per symbol (see sim_series.py).
- Executions follow the token's trading status: Ondo off-hours or closed, or a closed / no-liquidity token, is
  REJECTED with the real error code (quotes still work, as on the real API).
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
from backend.mocks import MockExchangeClient
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
from .sim_series import SymbolSeries, index_at, timeline
from .sim_universe import SIM_DEFAULT_VOLUME, SIM_UNIVERSE, SIM_UNIVERSE_REGIMES
from .tokens import DEFAULT_TOKENS
from .universe import merge_rows, rows_from_tokens
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
        # Real tokens the shared mocks don't have yet (NVDAB is the 24/7 demo token). SIM start prices:
        # NVDAB ~230.5 USDT from a real 25 USDT quote on 2026-10-11; TSLAon reuses the mock TSLA start (200).
        sim_start = {"NVDAB": 230.5, "TSLAon": 200.0}
        sim_regime = {"NVDAB": Regime.MILD_UPTREND, "TSLAon": Regime.HIGH_VOLATILITY}
        for sym, t in real.items():
            if sym not in self._tokens:
                self._tokens[sym] = t.model_copy(update={"price": sim_start.get(sym, 100.0)})
                self._regimes.setdefault(sym, sim_regime.get(sym, Regime.SIDEWAYS))
        self.enforce_market_hours = enforce_market_hours
        self.enforce_paths = enforce_paths
        self._latency = latency_s
        self._sleep = sleep
        self._replay_index: Optional[int] = None
        self._replay_time: Optional[datetime] = None
        self._series_cache: dict[tuple, list[Candle]] = {}
        self.enable_xstock = xstock_enabled() if enable_xstock is None else enable_xstock
        self._quote_meta = {}
        self._status_override: dict[str, TradingStatus] = {}
        # Wider universe (list_tokens / Auto-pick): resolvable everywhere, but not in search_tokens("").
        self._universe: dict[str, TokenInfo] = {t.symbol: t for t in SIM_UNIVERSE if t.symbol not in self._tokens}
        for sym in self._universe:
            self._regimes.setdefault(sym, SIM_UNIVERSE_REGIMES.get(sym, Regime.SIDEWAYS))
        self._live_limits: Optional[dict] = None

    # ------------------------------------------------------------------ tokens
    def _token(self, symbol: str) -> TokenInfo:
        if symbol in self._tokens:
            return self._tokens[symbol]
        if symbol in getattr(self, "_universe", {}):
            return self._universe[symbol]
        raise KeyError(f"unknown token {symbol!r}")

    def search_tokens(self, query: str) -> list[TokenInfo]:
        """Empty query: the default SIM tokens (unchanged). A named query also searches the wider SIM universe."""
        out = super().search_tokens(query)
        q = query.strip().lower()
        if q:
            for t in self._universe.values():
                if q in t.symbol.lower() or q in t.name.lower() or q in (t.underlying or "").lower():
                    out.append(t.model_copy(update={"price": self.get_price(t.symbol)}))
        return out

    def list_tokens(self, refresh: bool = False) -> list[dict]:
        """Same row shape as BinanceWeb3Client.list_tokens (see universe.py): the default SIM tokens plus the
        SIM universe of real bStock / Ondo tokens, with SIM prices and status (get_token_status)."""
        toks = list(self._tokens.values()) + list(self._universe.values())
        rows = merge_rows(rows_from_tokens(toks, "sim", self.enable_xstock))
        for r in rows:
            st = self.get_token_status(r["symbol"])
            tok = self._token(r["symbol"])
            vol = tok.volume_24h if tok.volume_24h is not None else SIM_DEFAULT_VOLUME.get(
                r["symbol"], 0.0 if is_xstock(r["symbol"], tok.token_type) else 50_000.0)
            r.update(price=st.price, volume_24h=vol, status=st.trading_status.value, tradable=st.tradable,
                     reason=st.reason, min_order_usdt=st.min_order_usdt, max_order_usdt=st.max_order_usdt,
                     reference_price=st.reference_price, shares_multiplier=st.shares_multiplier,
                     market_status=st.market_status)
        return rows

    # ------------------------------------------------------------------ runtime LIVE limits (SIM: stored only)
    def set_live_limits(self, max_order_usdt: float, max_sell_usdt: Optional[float] = None) -> dict:
        """Same validation as LIVE; SIM never sends real orders, so the value is only stored and echoed back."""
        from .limits import hard_max_order_usdt, validate_max_order_usdt

        buy = validate_max_order_usdt(max_order_usdt, hard_max_order_usdt())
        sell = validate_max_order_usdt(max_sell_usdt, None, "max_sell_usdt") if max_sell_usdt is not None else 2 * buy
        if sell < buy:
            raise ValueError(f"max_sell_usdt {sell:.2f} must be >= max_order_usdt {buy:.2f}")
        self._live_limits = {"max_order_usdt": buy, "max_sell_usdt": sell}
        return self.live_limits()

    def live_limits(self) -> dict:
        from .baw_executor import max_order_usdt as env_max_order
        from .limits import hard_max_order_usdt

        d = env_max_order()
        cur = self._live_limits or {"max_order_usdt": d, "max_sell_usdt": 2 * d}
        return {**cur, "max_live_notional": cur["max_sell_usdt"], "default_max_order_usdt": d,
                "hard_max_order_usdt": hard_max_order_usdt()}

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

    # ------------------------------------------------------------------ replay (time-based)
    @property
    def replay_index(self) -> Optional[int]:
        """Last index given to set_replay_index (None = not pinned by index). Kept for callers that read it back."""
        return self._replay_index

    @property
    def replay_time(self) -> Optional[datetime]:
        """The pinned SIM time (None = unpinned: full series, latest price)."""
        return self._replay_time

    def set_replay_time(self, t: Optional[datetime]) -> None:
        """Pin the SIM clock to `t`. Every timeframe then returns bars labelled <= t, and price / quotes / fills
        use the price at t (the same on every timeframe). None unpins."""
        self._replay_time = t
        if t is None:
            self._replay_index = None

    def set_replay_index(self, i: Optional[int], timeframe: Timeframe = Timeframe.H1) -> None:
        """Pin the clock to bar `i` (0 = oldest) of `timeframe`'s series, i.e. set_replay_time(that bar's time).

        Out-of-range i is clamped to [0, n_bars(tf)-1]; None unpins. The pin is a time, so switching timeframe
        keeps the clock where it is: candles on any timeframe stop at that time and the price does not jump.
        """
        if i is None:
            self.set_replay_time(None)
            return
        t0, step, count = timeline(timeframe, self._n)
        self._replay_index = int(i)
        self._replay_time = t0 + max(0, min(int(i), count - 1)) * step

    def n_bars(self, symbol: str, timeframe: Timeframe = Timeframe.H1) -> int:
        """Length of `timeframe`'s replayable series (H1 = the client's n_candles; finer timeframes have more bars
        over the same span, 4h / 1d have n_candles bars reaching further back)."""
        self._token(symbol)
        return timeline(timeframe, self._n)[2]

    def index_at(self, symbol: str, timeframe: Timeframe, t: datetime) -> int:
        """Index of the last `timeframe` bar labelled <= t (-1 if none). Use it to resume a replay on another
        timeframe at the same time."""
        self._token(symbol)
        return index_at(timeframe, self._n, t)

    def _sym_series(self, symbol: str) -> SymbolSeries:
        tok = self._token(symbol)
        regime = self._regimes.get(symbol, Regime.SIDEWAYS)
        key = (symbol, regime, tok.price)
        if key not in self._series_cache:
            self._series_cache[key] = SymbolSeries(symbol, regime, tok.price or 100.0, self._seed, self._n)
        return self._series_cache[key]

    def _series(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        return self._sym_series(symbol).candles(timeframe)

    def get_full_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1) -> list[Candle]:
        """The whole series for `timeframe`, ignoring the pin (history / backtests; safe while a loop is pinned)."""
        return list(self._series(symbol, timeframe))

    def get_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        series = self._series(symbol, timeframe)
        if self._replay_time is not None:
            series = series[: index_at(timeframe, self._n, self._replay_time) + 1] or series[:1]
        return list(series[-limit:]) if limit > 0 else []

    def get_price(self, symbol: str) -> float:
        if symbol in self._price_override:
            return self._price_override[symbol]
        ss = self._sym_series(symbol)
        if self._replay_time is None:
            return ss.h1[-1].close
        return ss.price_at(max(self._replay_time, ss.start))

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
        status = self.get_token_status(order.symbol).trading_status
        if status in (TradingStatus.CLOSED, TradingStatus.OFFHOURS) and tok.token_type in MARKET_CLOSED_CODE:
            # Real venue: Ondo off-hours (weekend reference-price session) / closed -> 40367, bStock -> 40369.
            code = MARKET_CLOSED_CODE[tok.token_type]
            return reject(f"{code} {order.symbol} is {status.value}: not tradable now (quote only)")
        if status == TradingStatus.NO_LIQUIDITY:
            return reject(f"{RWA_INSUFFICIENT_LIQUIDITY} {order.symbol}: no liquidity")
        return None

    def execute_swap(self, order: OrderRequest, quote: Quote, max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        self._maybe_sleep()
        return self._venue_check(order, quote, ExecutionPath.SWAP) or self._finish(order, quote, ExecutionPath.SWAP)

    def execute_rfq(self, order: OrderRequest, quote: Quote, max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        self._maybe_sleep()
        return self._venue_check(order, quote, ExecutionPath.RFQ) or self._finish(order, quote, ExecutionPath.RFQ)

    def _finish(self, order: OrderRequest, quote: Quote, path: ExecutionPath) -> ExecutionResult:
        res = self._execute(order, quote, path)
        if res.status == OrderStatus.FILLED and res.tx_hash:
            res = res.model_copy(update={"tx_hash": res.tx_hash.replace("0xmock", "0xsim", 1)})
            self.executions[-1] = res
        return res

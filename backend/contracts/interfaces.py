"""Abstract module interfaces. Each bot implements exactly one of these Protocols.

All methods are synchronous for simplicity (FastAPI runs sync endpoints in a threadpool).
Implementations must not raise for "normal" negative outcomes (e.g. stay out, blocked,
expired quote) - encode those in the returned model instead.
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence, Union, runtime_checkable

from .enums import OrderSide, PresetName, Regime, Timeframe, TraderStyle, TradingMode
from .models import (
    AutoConfig,
    BacktestResult,
    Candle,
    ControlState,
    Decision,
    ExecutionResult,
    ManualStrategy,
    OrderRequest,
    Portfolio,
    Quote,
    RegimeAnalysis,
    RiskVerdict,
    Signal,
    StatusSnapshot,
    StrategySelection,
    TokenInfo,
)


@runtime_checkable
class ExchangeClient(Protocol):
    """Bot 4 (backend/exchange). Binance Web3 API + Agentic Wallet, plus a SIM implementation."""

    @property
    def mode(self) -> TradingMode: ...

    def get_candles(self, symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        """Oldest first."""
        ...

    def get_price(self, symbol: str) -> float: ...

    def search_tokens(self, query: str) -> list[TokenInfo]:
        """RWA / tokenized stock search. Empty query returns the default list."""
        ...

    def get_quote(self, symbol: str, side: OrderSide, notional: float) -> Quote:
        """Quote valid for ~30s (Quote.expires_at)."""
        ...

    def execute_swap(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        """Execute via the SWAP path. Expired quote -> status EXPIRED (do not raise)."""
        ...

    def execute_rfq(self, order: OrderRequest, quote: Quote) -> ExecutionResult:
        """Execute via the RFQ path. Expired quote -> status EXPIRED (do not raise)."""
        ...


@runtime_checkable
class StrategyEngine(Protocol):
    """Bot 2 (backend/engine). Pure functions of candles + config; no I/O, no execution."""

    def detect_regime(self, candles: Sequence[Candle]) -> RegimeAnalysis: ...

    def select_strategy(
        self, regime: Regime, style: TraderStyle, enabled_presets: Sequence[PresetName]
    ) -> StrategySelection:
        """map(regime, style) -> exactly one preset, or preset=None (stay out)."""
        ...

    def evaluate_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], portfolio: Portfolio) -> Signal: ...

    def evaluate_auto(self, config: AutoConfig, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        """detect_regime -> select_strategy -> evaluate ONLY that preset -> BUY/SELL/STAY_OUT with reason."""
        ...

    def backtest_manual(
        self, strategy: ManualStrategy, candles: Sequence[Candle], initial_capital: float
    ) -> BacktestResult: ...


@runtime_checkable
class RiskManager(Protocol):
    """Bot 3 (backend/risk). Can always override and force STAY_OUT."""

    def check(self, signal: Signal, portfolio: Portfolio, control: ControlState) -> RiskVerdict: ...

    def check_exits(
        self, portfolio: Portfolio, prices: dict[str, float], bars: Optional[dict[str, Candle]] = None
    ) -> list[Signal]:
        """Stop-loss / take-profit / trailing-stop enforcement -> SELL signals (exit_reason, position_id, price=fill).

        `bars` (symbol -> latest bar) enables intrabar rules from ExitRules (SL wins if SL and TP both hit, fills at
        level or gap open). Without `bars`, evaluate on `prices` only. Must not mutate the portfolio; the app updates
        Position.highest_price from bar.high after this call."""
        ...

    def get_control_state(self) -> ControlState: ...

    def pause(self, reason: str = "") -> ControlState: ...

    def resume(self) -> ControlState:
        """Clears `paused` only. Does NOT clear an emergency stop (use reset_emergency_stop)."""
        ...

    def emergency_stop(self, reason: str = "") -> ControlState:
        """Sets emergency_stopped=True; blocks every signal until reset_emergency_stop()."""
        ...

    def reset_emergency_stop(self) -> ControlState:
        """Clears emergency_stopped (explicit user action). Leaves `paused` unchanged."""
        ...

    def set_confirmation_mode(self, enabled: bool) -> ControlState: ...


@runtime_checkable
class Explainer(Protocol):
    """Bot 5 (backend/llm). Grok-backed with deterministic template fallback. NEVER executes trades."""

    def explain_decision(self, decision: Decision) -> str: ...

    def summarize_state(self, status: StatusSnapshot) -> str: ...

    def parse_strategy_from_text(self, text: str, default_symbol: Optional[str] = None) -> Union[ManualStrategy, AutoConfig]: ...

    def answer(self, question: str, status: StatusSnapshot) -> str: ...

"""Pydantic v2 data contracts - the single source of truth for every module and the frontend.

Owned by the coordinator. Bots must NOT edit this file on their branches; request changes instead
(see CONTRIBUTING.md). After any change run `python scripts/gen_contracts.py` to regenerate
contracts/schema/*.json and frontend/src/contracts.ts.

Conventions:
- All timestamps are timezone-aware UTC datetimes (serialized as ISO-8601 strings).
- Money amounts are in the quote currency (USDT) unless the field name says otherwise.
- Percentages are expressed in percent units (2.5 means 2.5%), never fractions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    Action,
    ActivationMode,
    AgentMode,
    BreakoutDirection,
    ConditionLogic,
    AlertLevel,
    EventType,
    ExitReason,
    ExecutionPath,
    OrderSide,
    OrderStatus,
    PresetName,
    Regime,
    Timeframe,
    TokenType,
    TraderStyle,
    TradingMode,
)

CONTRACTS_VERSION = "0.2.0"
QUOTE_TTL_SECONDS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False, populate_by_name=True)


# --------------------------------------------------------------------------- market data
class Candle(ContractModel):
    timestamp: datetime = Field(description="Candle open time (UTC)")
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_ohlc(self) -> "Candle":
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("inconsistent OHLC: high must be the max and low the min")
        return self


class TokenInfo(ContractModel):
    symbol: str = Field(description="Trading symbol, e.g. NVDAon, TSLAx, AAPLB")
    name: str
    token_type: TokenType = TokenType.OTHER
    chain: str = Field(description="Chain name, e.g. BSC, Ethereum, Solana")
    chain_id: Optional[str] = None
    contract_address: Optional[str] = None
    underlying: Optional[str] = Field(default=None, description="Underlying equity ticker, e.g. NVDA")
    decimals: int = 18
    price: Optional[float] = Field(default=None, description="Last known price in USDT")
    volume_24h: Optional[float] = None


Token = TokenInfo  # alias


class Quote(ContractModel):
    quote_id: str = Field(default_factory=new_id)
    symbol: str
    side: OrderSide
    path: ExecutionPath = ExecutionPath.SWAP
    notional: float = Field(gt=0, description="Quote-currency amount (USDT) to spend (BUY) or receive (SELL)")
    quantity: float = Field(gt=0, description="Token quantity bought/sold at this quote")
    price: float = Field(gt=0)
    fee: float = Field(default=0.0, ge=0)
    slippage_bps: int = Field(default=50, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(seconds=QUOTE_TTL_SECONDS))

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or utc_now()) >= self.expires_at


# --------------------------------------------------------------------------- orders & portfolio
class OrderRequest(ContractModel):
    client_order_id: str = Field(default_factory=new_id)
    symbol: str
    side: OrderSide
    notional: float = Field(gt=0, description="USDT amount for BUY; for SELL the USDT value to sell")
    quantity: Optional[float] = Field(default=None, gt=0, description="Exact token qty (SELL); overrides notional")
    path: ExecutionPath = ExecutionPath.SWAP
    quote_id: Optional[str] = None
    slippage_bps: int = Field(default=50, ge=0)
    mode: TradingMode = TradingMode.SIM
    decision_id: Optional[str] = None


class ExecutionResult(ContractModel):
    order_id: str = Field(default_factory=new_id)
    client_order_id: str
    symbol: str
    side: OrderSide
    status: OrderStatus
    path: ExecutionPath
    mode: TradingMode
    filled_quantity: float = Field(default=0.0, ge=0)
    avg_price: Optional[float] = None
    notional: float = Field(default=0.0, ge=0)
    fee: float = Field(default=0.0, ge=0)
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)


class Position(ContractModel):
    """An open position. `highest_price` is maintained by backend/app: after RiskManager.check_exits runs for a bar,
    the app sets highest_price = max(highest_price or avg_entry_price, bar.high) for every still-open position."""

    id: str = Field(default_factory=new_id)
    symbol: str
    quantity: float = Field(gt=0)
    avg_entry_price: float = Field(gt=0)
    opened_at: datetime = Field(default_factory=utc_now)
    strategy: str = Field(description="Strategy that opened it: preset name or 'MANUAL:<strategy name>'")
    current_price: Optional[float] = None
    highest_price: Optional[float] = Field(default=None, description="High-water mark since entry (trailing stop); updated by the app from bar.high AFTER check_exits")
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    unrealized_pnl: float = 0.0


class Portfolio(ContractModel):
    starting_capital: float = Field(gt=0)
    cash: float = Field(ge=0)
    positions: list[Position] = Field(default_factory=list)
    realized_pnl: float = 0.0
    equity: float = Field(ge=0, description="cash + market value of positions")
    bars_since_exit: dict[str, int] = Field(default_factory=dict, description="Per symbol, bars since last exit (cooldown)")
    trades_today: int = 0
    daily_pnl: float = 0.0
    mode: TradingMode = TradingMode.SIM
    updated_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------- manual strategy (spec 2.1)
# Field names, defaults and semantics mirror reference/full_strategy_builder_simulation.html (the
# "simulator"). The simulator DOM id for each field is noted as `sim:<id>`. Deviations are listed in
# reference/README.md. Bot 2's evaluator must follow the semantics in these docstrings.
#
# Indicator definitions (simulator): SMA = mean of closes; EMA seeded with SMA of the first `period`
# closes, k = 2/(period+1); RSI = Wilder-style; MACD line = EMA12 - EMA26, signal = EMA of the line with
# alpha 0.15 seeded with the first line value; Bollinger = SMA20 +/- 2 * population std-dev.

SIM_WARMUP_BARS = 60  # simulator starts evaluating at bar index 60
DEFAULT_INITIAL_CAPITAL = 10000.0  # sim:capital


class RsiBelowCondition(ContractModel):
    """sim:useRSI / sim:rsiEntry. True when RSI(period) < threshold."""

    enabled: bool = True
    period: int = Field(default=14, ge=2)
    threshold: float = Field(default=35.0, ge=0, le=100)


class PriceAboveSmaCondition(ContractModel):
    """sim:useSMA / sim:smaPeriod. True when close > SMA(period)."""

    enabled: bool = True
    period: int = Field(default=50, ge=2)


class PriceAboveEmaCondition(ContractModel):
    """sim:useEMA / sim:emaPeriod. True when close > EMA(period)."""

    enabled: bool = False
    period: int = Field(default=20, ge=2)


class MacdParams(ContractModel):
    fast: int = Field(default=12, ge=1)
    slow: int = Field(default=26, ge=2)
    signal: int = Field(default=9, ge=1, description="Simulator approximates a 9-period signal EMA with alpha=0.15")


class MacdCrossCondition(ContractModel):
    """Entry (sim:useMACD): line crosses ABOVE signal this bar (line[i] > sig[i] and line[i-1] <= sig[i-1]).
    Exit (sim:useMacdExit): line crosses BELOW signal (line[i] < sig[i] and line[i-1] > sig[i-1])."""

    enabled: bool = False
    params: MacdParams = Field(default_factory=MacdParams)


class BollingerLowerCondition(ContractModel):
    """sim:useBB. True when close < lower_band * (1 + proximity_pct/100) (i.e. near OR below the band)."""

    enabled: bool = False
    period: int = Field(default=20, ge=2)
    std_dev: float = Field(default=2.0, gt=0)
    proximity_pct: float = Field(default=2.0, ge=0, description="Simulator uses close < lower * 1.02")


class VolumeAboveAverageCondition(ContractModel):
    """sim:useVolume. True when volume > multiplier * average volume.
    period=None means 'average over all candles passed to the evaluator' (simulator behaviour)."""

    enabled: bool = False
    period: Optional[int] = Field(default=None, ge=2)
    multiplier: float = Field(default=1.1, gt=0)


class BreakoutCondition(ContractModel):
    """PDF-only (not in the simulator). HIGH: close > max(high of previous `lookback` bars);
    LOW: close < min(low of previous `lookback` bars)."""

    enabled: bool = False
    lookback: int = Field(default=20, ge=2)
    direction: BreakoutDirection = BreakoutDirection.HIGH


class EntryConditions(ContractModel):
    """Combination semantics (simulator): each ENABLED condition whose indicator is available (not in warm-up)
    contributes one boolean; conditions whose indicator is still null are skipped, not counted as false.
    If no booleans were produced -> no entry. AND = all true (sim: every), OR = at least one true (sim: some).
    Canonical names for Signal.conditions_met/failed are the field names below."""

    logic: ConditionLogic = ConditionLogic.AND  # sim:entryLogic
    rsi_below: RsiBelowCondition = Field(default_factory=RsiBelowCondition)
    price_above_sma: PriceAboveSmaCondition = Field(default_factory=PriceAboveSmaCondition)
    price_above_ema: PriceAboveEmaCondition = Field(default_factory=PriceAboveEmaCondition)
    macd_bullish_cross: MacdCrossCondition = Field(default_factory=MacdCrossCondition)
    bollinger_lower: BollingerLowerCondition = Field(default_factory=BollingerLowerCondition)
    volume_above_avg: VolumeAboveAverageCondition = Field(default_factory=VolumeAboveAverageCondition)
    breakout: BreakoutCondition = Field(default_factory=BreakoutCondition)


class TrailingStopRule(ContractModel):
    """sim:useTrail / sim:trail. Stop starts at entry*(1-pct/100), ratchets up to close*(1-pct/100);
    exit when close < stop."""

    enabled: bool = False
    pct: float = Field(default=4.0, gt=0)


class RsiExitRule(ContractModel):
    """sim:useRsiExit / sim:rsiExit. Exit when RSI(period) > threshold."""

    enabled: bool = True
    period: int = Field(default=14, ge=2)
    threshold: float = Field(default=70.0, ge=0, le=100)


class ExitRules(ContractModel):
    """Evaluated on each bar BEFORE entries; first match wins. Simulator order:
    TRAILING_STOP -> TAKE_PROFIT (pnl >= tp) -> STOP_LOSS (pnl <= -sl) -> RSI_EXIT -> MACD_EXIT.

    Intrabar rules for the engine / risk layer (contract v0.2, deviates from the close-only simulator):
    - If the bar's range hits BOTH the stop-loss and the take-profit level, STOP_LOSS wins (conservative).
    - SL / TP / trailing fill AT the level, or at the bar OPEN if the bar gapped through the level
      (SL/trailing: fill = min(level, open) when open < level; TP: fill = max(level, open) when open > level).
    - The trailing level uses Position.highest_price as of the PREVIOUS bar (the app updates it from bar.high after
      check_exits), so a bar's own high never tightens its own stop.
    - Open positions are closed with reason END at the end of a backtest."""

    take_profit_pct: float = Field(default=12.0, gt=0)  # sim:tp
    stop_loss_pct: float = Field(default=6.0, gt=0)  # sim:sl
    trailing_stop: TrailingStopRule = Field(default_factory=TrailingStopRule)
    rsi_above: RsiExitRule = Field(default_factory=RsiExitRule)
    macd_bearish_cross: MacdCrossCondition = Field(default_factory=MacdCrossCondition)  # sim:useMacdExit


class PositionRiskRules(ContractModel):
    """Entry allowed only if open_positions < max_open_positions AND bars_since_last_exit > cooldown_bars."""

    position_size_pct: float = Field(default=15.0, gt=0, le=100, description="% of current capital per entry (sim:size)")
    max_open_positions: int = Field(default=1, ge=1, description="sim:maxPos (UI allows 1-3)")
    cooldown_bars: int = Field(default=5, ge=0, description="sim:cooldown")
    max_loss_per_trade_pct: Optional[float] = Field(default=None, gt=0, description="PDF-only; % of capital")


class MarketHoursFilter(ContractModel):
    """sim:marketHours. Real engine: only trade when bar time is within [start, end) UTC (weekdays if set).
    The simulator fakes this as bar_index % 24 < 16."""

    enabled: bool = False
    start_hour_utc: int = Field(default=13, ge=0, le=23)
    end_hour_utc: int = Field(default=20, ge=0, le=24)
    weekdays_only: bool = True


class MinVolumeFilter(ContractModel):
    """sim:minVol ('Require above-average volume'). Hard gate: volume > multiplier * average volume
    (same average as VolumeAboveAverageCondition) and, if set, volume >= min_volume (absolute, PDF)."""

    enabled: bool = False
    multiplier: float = Field(default=1.1, gt=0)
    min_volume: Optional[float] = Field(default=None, ge=0)


class StrategyFilters(ContractModel):
    market_hours: MarketHoursFilter = Field(default_factory=MarketHoursFilter)
    min_volume: MinVolumeFilter = Field(default_factory=MinVolumeFilter)


class AutoPickConfig(ContractModel):
    """Watchlist Auto-pick (v0.3): pick up to ``max_tokens`` tradable tokens of one narrative by momentum/trend/liquidity."""

    enabled: bool = False
    max_tokens: int = Field(default=3, ge=1, le=5)
    narrative: Literal["AI_CHIPS", "EV_ENERGY", "BIG_TECH", "CRYPTO", "ANY"] = "ANY"


class ManualStrategy(ContractModel):
    """A user-built rule set (spec 2.1/2.2). Saved and exchanged as JSON. Defaults == simulator defaults."""

    id: str = Field(default_factory=new_id)
    name: str = "My Strategy"
    symbol: str = Field(default="NVDAon", description="Token symbol (sim:token): NVDAon, TSLAx, AAPLB, ...")
    timeframe: Timeframe = Timeframe.H1
    entry: EntryConditions = Field(default_factory=EntryConditions)
    exit: ExitRules = Field(default_factory=ExitRules)
    risk: PositionRiskRules = Field(default_factory=PositionRiskRules)
    filters: StrategyFilters = Field(default_factory=StrategyFilters)
    activation: ActivationMode = ActivationMode.CONFIRMATION
    created_at: datetime = Field(default_factory=utc_now)
    version: int = 1
    watchlist: list[str] = Field(default_factory=list, max_length=5, description=(
        "v0.3: up to 5 tokens traded with these rules; empty = [symbol]. `symbol` stays the first entry"))
    auto_pick: Optional[AutoPickConfig] = Field(default=None, description="v0.3: pick the watchlist automatically")


# --------------------------------------------------------------------------- auto mode (spec 3)
class AutoConfig(ContractModel):
    symbol: str
    style: TraderStyle = TraderStyle.BALANCED
    enabled_presets: list[PresetName] = Field(default_factory=lambda: list(PresetName), min_length=1)
    capital: float = Field(gt=0, description="USDT allocated to the auto agent")
    timeframe: Timeframe = Timeframe.H1
    activation: ActivationMode = ActivationMode.CONFIRMATION
    watchlist: list[str] = Field(default_factory=list, max_length=5, description=(
        "v0.3: up to 5 tokens for the auto agent; empty = [symbol]. `symbol` stays the first entry"))
    auto_pick: Optional[AutoPickConfig] = Field(default=None, description="v0.3: pick the watchlist automatically")


class RegimeAnalysis(ContractModel):
    regime: Regime
    confidence: float = Field(ge=0, le=1)
    reason: str
    features: dict[str, float] = Field(default_factory=dict, description="e.g. sma_slope, rsi, atr_pct, dist_from_ema_pct")
    timestamp: datetime = Field(default_factory=utc_now)


class StrategySelection(ContractModel):
    """Result of map(regime, style) -> exactly one strategy (or none => stay out)."""

    regime: Regime
    style: TraderStyle
    preset: Optional[PresetName] = Field(default=None, description="None means 'pause / stay out' for this regime")
    size_multiplier: float = Field(default=1.0, ge=0, le=1, description="e.g. 0.5 in high volatility")
    reason: str


# --------------------------------------------------------------------------- decision pipeline
class Signal(ContractModel):
    """Proposed action from the strategy engine (Bot 2) or an exit from RiskManager.check_exits (Bot 3).
    Never executed without a RiskVerdict. For SELL signals `price` is the intended fill price (e.g. the SL/TP level
    or gap-open price per ExitRules), `exit_reason` says why, and `position_id` targets a specific Position."""

    id: str = Field(default_factory=new_id)
    symbol: str
    action: Action
    source: AgentMode
    strategy: str = Field(description="Preset name (AUTO) or 'MANUAL:<name>'")
    preset: Optional[PresetName] = None
    regime: Optional[Regime] = None
    reason: str
    conditions_met: list[str] = Field(default_factory=list)
    conditions_failed: list[str] = Field(default_factory=list)
    price: Optional[float] = Field(default=None, description="BUY: reference price; SELL: fill price (level or gap open)")
    exit_reason: Optional[ExitReason] = None
    position_id: Optional[str] = Field(default=None, description="Position.id this SELL closes")
    proposed_size_pct: float = Field(default=0.0, ge=0, le=100)
    proposed_notional: float = Field(default=0.0, ge=0)
    timestamp: datetime = Field(default_factory=utc_now)


class RiskCheck(ContractModel):
    name: str = Field(description="e.g. max_exposure, cooldown, paused, max_open_positions")
    passed: bool
    detail: str = ""


class RiskVerdict(ContractModel):
    signal_id: str
    approved: bool
    reasons: list[str] = Field(default_factory=list, description="Human readable; required when blocked")
    checks: list[RiskCheck] = Field(default_factory=list)
    adjusted_size_pct: float = Field(default=0.0, ge=0, le=100)
    adjusted_notional: float = Field(default=0.0, ge=0)
    requires_confirmation: bool = False
    timestamp: datetime = Field(default_factory=utc_now)


class Decision(ContractModel):
    """Final, fully explainable record of one evaluation cycle (regime + strategy + reason)."""

    id: str = Field(default_factory=new_id)
    timestamp: datetime = Field(default_factory=utc_now)
    mode: TradingMode
    agent_mode: AgentMode
    symbol: str
    regime: Optional[Regime] = None
    strategy: str
    action: Action
    reason: str
    signal: Optional[Signal] = None
    risk_verdict: Optional[RiskVerdict] = None
    execution: Optional[ExecutionResult] = None
    explanation: Optional[str] = Field(default=None, description="Plain-language text from the LLM layer (Bot 5)")


class ControlState(ContractModel):
    paused: bool = False
    emergency_stopped: bool = False
    confirmation_mode: bool = True
    trading_mode: TradingMode = TradingMode.SIM
    reason: Optional[str] = None
    updated_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------- backtest / status / events
class BacktestTrade(ContractModel):
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    quantity: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: Optional[ExitReason] = None


class EquityPoint(ContractModel):
    timestamp: datetime
    equity: float


class BacktestResult(ContractModel):
    strategy_name: str
    symbol: str
    initial_capital: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    num_trades: int
    trades: list[BacktestTrade] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)


class StatusSnapshot(ContractModel):
    """Everything the dashboard / explainer needs in one object."""

    control: ControlState
    portfolio: Portfolio
    agent_mode: Optional[AgentMode] = None
    active_symbol: Optional[str] = None
    current_regime: Optional[Regime] = None
    active_strategy: Optional[str] = None
    last_decision: Optional[Decision] = None
    recent_decisions: list[Decision] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utc_now)


class Alert(ContractModel):
    id: str = Field(default_factory=new_id)
    level: AlertLevel
    code: str = Field(description="EMERGENCY_STOP, PAUSED, CONSECUTIVE_LOSSES, DAILY_LOSS, QUOTE_EXPIRED, EXECUTION_FAILED, RISK_BLOCKED, LIVE_MODE, COMPONENT_FALLBACK")
    message: str
    symbol: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)


class PerformanceSummary(ContractModel):
    starting_capital: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_return_pct: float
    max_drawdown_pct: float = Field(description="Over the mark-to-market equity curve (open positions included)")
    win_rate_pct: float
    num_trades: int = Field(description="Closed trades")
    wins: int
    losses: int
    consecutive_losses: int
    equity_curve: list[EquityPoint] = Field(default_factory=list)


class SystemInfo(ContractModel):
    trading_mode: TradingMode
    live_enabled: bool
    contracts_version: str
    sim_seed: Optional[int] = None
    components: dict[str, str]


class LoopState(ContractModel):
    running: bool
    interval_seconds: float
    ticks: int
    agent_mode: Optional[AgentMode] = None
    active_symbol: Optional[str] = None
    active_strategy: Optional[str] = None
    replay_finished: bool = False


class BacktestRequest(ContractModel):
    strategy: ManualStrategy
    initial_capital: float = Field(default=DEFAULT_INITIAL_CAPITAL, gt=0)
    limit: int = Field(default=500, ge=61, le=5000)


class Event(ContractModel):
    """Envelope for WebSocket messages and structured logs."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


ALL_MODELS: list[type[BaseModel]] = [
    Candle, TokenInfo, Quote, OrderRequest, ExecutionResult, Position, Portfolio,
    RsiBelowCondition, PriceAboveSmaCondition, PriceAboveEmaCondition, MacdParams, MacdCrossCondition,
    BollingerLowerCondition, VolumeAboveAverageCondition, BreakoutCondition, EntryConditions,
    TrailingStopRule, RsiExitRule, ExitRules, PositionRiskRules, MarketHoursFilter, MinVolumeFilter, StrategyFilters,
    AutoPickConfig, ManualStrategy, AutoConfig, RegimeAnalysis, StrategySelection, Signal, RiskCheck, RiskVerdict,
    Decision, ControlState, BacktestTrade, EquityPoint, BacktestResult, StatusSnapshot,
    Alert, PerformanceSummary, SystemInfo, LoopState, BacktestRequest, Event,
]

"""Enumerations shared by every module. Owned by the coordinator - do not edit from a bot branch."""
from __future__ import annotations

from enum import Enum


class TradingMode(str, Enum):
    """Execution environment. SIM = mocks/offline, PAPER = real market data + simulated fills, LIVE = real money."""

    SIM = "SIM"
    PAPER = "PAPER"
    LIVE = "LIVE"


class AgentMode(str, Enum):
    """Which brain produced a signal: the user's rule set (MANUAL) or the regime-driven preset selector (AUTO)."""

    MANUAL = "MANUAL"
    AUTO = "AUTO"


class ActivationMode(str, Enum):
    """How an approved signal is executed. AUTO_EXECUTE = execute immediately, CONFIRMATION = wait for the user."""

    AUTO_EXECUTE = "AUTO_EXECUTE"
    CONFIRMATION = "CONFIRMATION"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    STAY_OUT = "STAY_OUT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Regime(str, Enum):
    """The six market regimes from spec section 3.1."""

    STRONG_UPTREND = "STRONG_UPTREND"  # price well above rising EMA/SMA -> trend following / momentum
    MILD_UPTREND = "MILD_UPTREND"  # price above SMA, not overextended -> smart DCA or light trend
    SIDEWAYS = "SIDEWAYS"  # low volatility, flat MAs -> grid or regular DCA
    OVERSOLD_PULLBACK = "OVERSOLD_PULLBACK"  # sharp drop + low RSI -> buy the dip
    HIGH_VOLATILITY = "HIGH_VOLATILITY"  # large swings, no direction -> reduce size or pause
    DOWNTREND = "DOWNTREND"  # price below falling MA -> pause or minimal DCA only


class TraderStyle(str, Enum):
    PATIENT = "PATIENT"  # Patient / Accumulator
    BALANCED = "BALANCED"  # Balanced / Swing
    ACTIVE = "ACTIVE"  # Active / Scalp-Spot


class PresetName(str, Enum):
    DCA = "DCA"
    SMART_DCA = "SMART_DCA"
    BUY_THE_DIP = "BUY_THE_DIP"
    TREND_FOLLOWING = "TREND_FOLLOWING"
    GRID = "GRID"


class ConditionLogic(str, Enum):
    AND = "AND"
    OR = "OR"


class BreakoutDirection(str, Enum):
    """Price-action breakout: break of the recent HIGH (bullish) or recent LOW."""

    HIGH = "HIGH"
    LOW = "LOW"


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class TokenType(str, Enum):
    """Issuer family of a tokenized stock."""

    ONDO = "ONDO"  # e.g. NVDAon
    XSTOCK = "XSTOCK"  # e.g. TSLAx
    BSTOCK = "BSTOCK"  # e.g. AAPLB
    OTHER = "OTHER"


class ExecutionPath(str, Enum):
    SWAP = "SWAP"
    RFQ = "RFQ"


class OrderStatus(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class EventType(str, Enum):
    """WebSocket / log event kinds pushed from backend/app to the frontend."""

    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    CONTROL = "CONTROL"
    PRICE = "PRICE"
    ALERT = "ALERT"
    STATUS = "STATUS"


class ExitReason(str, Enum):
    """Why a position was closed. First five mirror the simulator's reasons in priority order."""

    TRAILING_STOP = "TRAILING_STOP"  # sim: 'Trailing Stop'
    TAKE_PROFIT = "TAKE_PROFIT"  # sim: 'Take Profit'
    STOP_LOSS = "STOP_LOSS"  # sim: 'Stop Loss'
    RSI_EXIT = "RSI_EXIT"  # sim: 'RSI Exit'
    MACD_EXIT = "MACD_EXIT"  # sim: 'MACD Exit'
    END = "END"  # sim: 'End' (backtest finished)
    MAX_LOSS = "MAX_LOSS"  # risk layer: max loss per trade
    MANUAL = "MANUAL"  # user override
    EMERGENCY_STOP = "EMERGENCY_STOP"


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

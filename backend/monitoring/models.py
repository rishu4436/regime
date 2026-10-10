"""Monitoring data shapes not (yet) in backend/contracts. Proposed as contract additions."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.contracts import EquityPoint, new_id, utc_now
from datetime import datetime


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    level: AlertLevel
    code: str = Field(description="EMERGENCY_STOP, PAUSED, CONSECUTIVE_LOSSES, DAILY_LOSS, QUOTE_EXPIRED, EXECUTION_FAILED, RISK_BLOCKED, LIVE_MODE, COMPONENT_FALLBACK")
    message: str
    symbol: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)


class PerformanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
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

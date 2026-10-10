"""Request/response shapes that are not (yet) contract models. Proposed as contract additions in API.md."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from backend.contracts import AgentMode, AutoConfig, DEFAULT_INITIAL_CAPITAL, ManualStrategy, TradingMode


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemInfo(_M):
    trading_mode: TradingMode
    live_enabled: bool
    contracts_version: str
    sim_seed: Optional[int] = None
    components: dict[str, str]


class LoopState(_M):
    running: bool
    interval_seconds: float
    ticks: int
    agent_mode: Optional[AgentMode] = None
    active_symbol: Optional[str] = None
    active_strategy: Optional[str] = None
    replay_finished: bool = False


class BacktestRequest(_M):
    strategy: ManualStrategy
    initial_capital: float = Field(default=DEFAULT_INITIAL_CAPITAL, gt=0)
    limit: int = Field(default=500, ge=61, le=5000)


class ManualActivateRequest(_M):
    strategy_id: Optional[str] = None
    strategy: Optional[ManualStrategy] = None


class LoopStartRequest(_M):
    interval_seconds: Optional[float] = Field(default=None, gt=0)


class ReasonRequest(_M):
    reason: str = ""


class EmergencyStopRequest(_M):
    reason: str = ""
    flatten: bool = False


class ConfirmationModeRequest(_M):
    enabled: bool


class ParseRequest(_M):
    text: str
    default_symbol: Optional[str] = None


class ParseResponse(_M):
    kind: Literal["MANUAL", "AUTO"]
    strategy: Optional[ManualStrategy] = None
    config: Optional[AutoConfig] = None


class AskRequest(_M):
    question: str

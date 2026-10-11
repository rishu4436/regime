"""App-only request/response bodies. SystemInfo / LoopState / BacktestRequest / Alert / PerformanceSummary live in backend.contracts (v0.2)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer  # noqa: F401

from backend.contracts import AutoConfig, ManualStrategy, PresetName, Regime, TraderStyle


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class PresetsTableEntry(_M):
    regime: Regime
    style: TraderStyle
    preset: Optional[PresetName] = Field(default=None, description="None = stay out / pause")
    size_multiplier: float
    reason: str


class PresetsTable(_M):
    """map(regime, style) -> preset for all 6 x 3 combinations, all presets enabled.
    source = "engine:<class>" (the wired engine's select_strategy) or "fallback:<class>" (app stand-in)."""

    source: str
    entries: list[PresetsTableEntry]


# ---------------------------------------------------------------- LIVE / wallet (app-side until the contract has them)
from datetime import datetime  # noqa: E402

from backend.contracts import Decision, ExecutionResult, LoopState, StatusSnapshot, SystemInfo  # noqa: E402


class ExecutionView(ExecutionResult):
    """ExecutionResult + LIVE-only UI fields. Keys are only emitted when set, so SIM/PAPER payloads stay identical to
    the contract model. Contract request: add these as optional fields to ExecutionResult."""
    explorer_url: Optional[str] = None       # https://bscscan.com/tx/<tx_hash>
    route: Optional[str] = None              # 'SWAP' | 'RFQ' (= path)
    quote_expires_at: Optional[str] = None   # expiry of the quote that was executed
    real_funds: Optional[bool] = None        # true in LIVE

    @model_serializer(mode="wrap")
    def _drop_unset_live_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for k in ("explorer_url", "route", "quote_expires_at", "real_funds"):
                if data.get(k) is None:
                    data.pop(k, None)
        return data


class DecisionView(Decision):
    execution: Optional[ExecutionView] = None


class StatusView(StatusSnapshot):
    last_decision: Optional[DecisionView] = None
    recent_decisions: list[DecisionView] = Field(default_factory=list)


class LiveStatus(_M):
    live_trading_enabled: bool
    binance_live_enabled: bool
    risk_allow_live: bool
    wallet_connected: bool
    can_trade: bool
    blocked_reasons: list[str] = Field(default_factory=list)


class SystemInfoView(SystemInfo):
    """SystemInfo + `live` block. Contract request: add `live: Optional[LiveStatus]` to SystemInfo."""
    live: LiveStatus


class WalletBalance(_M):
    symbol: str
    amount: float
    usd_value: Optional[float] = None


class WalletInfo(_M):
    """Bot 1 shape. limits always has daily_limit_usdt, daily_used_usdt, per_order_cap_usdt, allowed_tokens (+ any
    keys Bot 3's risk.live_limits() returns, e.g. daily_remaining_usdt, limit_source)."""
    provider: str            # 'baw' (LIVE), 'web3_api', 'paper', 'sim'
    address: Optional[str] = None
    chain: str = "BSC"
    chain_id: int = 56
    connected: bool
    balances: list[WalletBalance] = Field(default_factory=list)
    bnb_gas_reserve: Optional[float] = None
    limits: dict
    updated_at: datetime


class Heartbeat(_M):
    started_at: datetime
    uptime_s: float
    last_tick_at: Optional[datetime] = None
    seconds_since_tick: Optional[float] = None
    restarts: int = 0
    last_error: Optional[str] = None
    autostart: bool = False


class LoopStateView(LoopState):
    heartbeat: Heartbeat

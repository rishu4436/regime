"""App-only request/response bodies. SystemInfo / LoopState / BacktestRequest / Alert / PerformanceSummary live in backend.contracts (v0.2)."""
from __future__ import annotations

from typing import Any, Literal, Optional

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
    notes: list[str] = Field(default_factory=list)  # Bot 5 parser notes (e.g. the default entry rule it filled in)


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

from backend.contracts import Decision, ExecutionResult, LoopState, Portfolio, Position, StatusSnapshot, SystemInfo, TokenInfo  # noqa: E402


class ExecutionView(ExecutionResult):
    """ExecutionResult + LIVE-only UI fields. Keys are only emitted when set, so SIM/PAPER payloads stay identical to
    the contract model. Contract request: add these as optional fields to ExecutionResult."""
    explorer_url: Optional[str] = None       # https://bscscan.com/tx/<tx_hash>
    route: Optional[str] = None              # 'SWAP' | 'RFQ' (= path)
    quote_expires_at: Optional[str] = None   # expiry of the quote that was executed
    real_funds: Optional[bool] = None        # true in LIVE
    gas: Optional[Any] = None                # Bot 4 executor extras (real gas from tx-history)
    price_impact_pct: Optional[float] = None  # percent units, from the executed quote / executor
    wallet_order_id: Optional[str] = None    # baw market-order id

    @model_serializer(mode="wrap")
    def _drop_unset_live_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for k in ("explorer_url", "route", "quote_expires_at", "real_funds", "gas", "price_impact_pct", "wallet_order_id"):
                if data.get(k) is None:
                    data.pop(k, None)
        return data


class DecisionView(Decision):
    execution: Optional[ExecutionView] = None
    awaiting_decision_id: Optional[str] = Field(default=None, description="STAY_OUT while a BUY waits for approval: "
                                                "id of that pending BUY decision (the reason text has no id)")

    @model_serializer(mode="wrap")
    def _drop_unset_awaiting(self, handler):  # only emitted when set: other decisions stay contract-shaped
        data = handler(self)
        if isinstance(data, dict) and data.get("awaiting_decision_id") is None:
            data.pop("awaiting_decision_id", None)
        return data


class StatusView(StatusSnapshot):
    portfolio: "PortfolioView"
    last_decision: Optional[DecisionView] = None
    recent_decisions: list[DecisionView] = Field(default_factory=list)


class LiveStatus(_M):
    live_trading_enabled: bool
    binance_live_enabled: bool
    risk_allow_live: bool
    wallet_connected: bool
    can_trade: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    cooldowns: list[dict] = Field(default_factory=list, description="[{symbol, until, code, reason}] after 40367/40369/40374")
    chain_pending: list[dict] = Field(default_factory=list, description="BUYs submitted and waiting on chain")
    wallet_block: Optional[dict] = Field(default=None, description="v0.8 {address, expected, since}: a different wallet signed "
                                         "in; no orders until POST /api/wallet/reconcile")
    session_expired: bool = Field(default=False, description="LIVE wallet session expired (latest wallet read): can_trade false, "
                                  "exits held until the wallet is signed in again (Reconnect wallet)")
    wallet_read_at: Optional[datetime] = Field(default=None, description="v0.9 LIVE: time of the last successful wallet read "
                                               "(served from cache; this endpoint never calls baw)")
    wallet_age_s: Optional[float] = Field(default=None, description="v0.9 LIVE: seconds since wallet_read_at")
    wallet_stale: bool = Field(default=False, description="v0.9 LIVE: the last wallet read failed for a network reason; the "
                               "last good read is shown, BUYs are blocked until a fresh read succeeds, exits still go out")


class WalletConnectStart(_M):
    session_id: str
    qr_url: str
    pairing_code: Optional[str] = None
    expires_at: Optional[str] = None


class WalletConnectStatus(_M):
    status: str = Field(description="pending | connected | expired | failed")
    address: Optional[str] = None
    wallet_changed: bool = False
    error: Optional[str] = None


class WalletReconcileResult(_M):
    address: Optional[str] = None
    was_blocked: bool
    kept: list[dict] = Field(default_factory=list)
    dropped: list[dict] = Field(default_factory=list)
    wallet_blocked: bool = False


class SystemInfoView(SystemInfo):
    """SystemInfo + `live` block. Contract request: add `live: Optional[LiveStatus]` to SystemInfo."""
    live: LiveStatus
    agent_identity: Optional[dict] = Field(default=None, description="Bot 5 agent_studio: {erc8004_id, registry, studio_version}")


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
    session_expired: bool = Field(default=False, description="Agentic Wallet session expired (e.g. `baw wallet status` -> "
                                  "'illegal parameter'): the dashboard shows the red banner and Reconnect wallet")
    read_at: Optional[datetime] = Field(default=None, description="v0.9 LIVE: time of the last successful wallet read (cached; "
                                        "GET /api/wallet never calls baw)")
    age_s: Optional[float] = Field(default=None, description="v0.9 LIVE: seconds since read_at")
    stale: bool = Field(default=False, description="v0.9 LIVE: the last read attempt failed (network); values are from read_at")
    read_error: Optional[str] = Field(default=None, description="v0.9 LIVE: why the last read attempt failed (stale only)")
    next_retry_at: Optional[datetime] = Field(default=None, description="v0.9 LIVE: next wallet read after a network failure")


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


class PositionView(Position):
    """Position + in-flight flags (Bot 1). pending=true: a LIVE BUY submitted but not yet settled on chain (synthetic
    position at the quote amount, counted by engine and risk); held_exit = {reason, source} while an exit waits for it."""
    pending: bool = False
    client_order_id: Optional[str] = None
    held_exit: Optional[dict] = None


class PortfolioView(Portfolio):
    positions: list[PositionView] = Field(default_factory=list)


class TokenView(TokenInfo):
    """TokenInfo + token_status (Bot 4 get_token_status as JSON: trading_status, min_order_usdt, reference_price_only,
    ...); null when the exchange has no status for it."""
    token_status: Optional[dict] = None


StatusView.model_rebuild()

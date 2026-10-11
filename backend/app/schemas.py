"""App-only request/response bodies. SystemInfo / LoopState / BacktestRequest / Alert / PerformanceSummary live in backend.contracts (v0.2)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field  # noqa: F401

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

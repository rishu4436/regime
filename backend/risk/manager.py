"""Skeleton. Bot 3 replaces the NotImplementedError bodies. Keep the class name/constructor stable for Bot 6."""
from __future__ import annotations

from backend.contracts import ControlState, Portfolio, RiskVerdict, Signal


class DefaultRiskManager:
    def __init__(self, control: ControlState | None = None) -> None:
        self._control = control or ControlState()

    def check(self, signal: Signal, portfolio: Portfolio, control: ControlState) -> RiskVerdict:
        raise NotImplementedError

    def check_exits(self, portfolio: Portfolio, prices: dict[str, float]) -> list[Signal]:
        raise NotImplementedError

    def get_control_state(self) -> ControlState:
        return self._control

    def pause(self, reason: str = "") -> ControlState:
        raise NotImplementedError

    def resume(self) -> ControlState:
        raise NotImplementedError

    def emergency_stop(self, reason: str = "") -> ControlState:
        raise NotImplementedError

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        raise NotImplementedError

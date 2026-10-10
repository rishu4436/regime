"""Skeleton. Bot 5 replaces the NotImplementedError bodies. Must work without XAI_API_KEY (template fallback)."""
from __future__ import annotations

from typing import Optional, Union

from backend.contracts import AutoConfig, Decision, ManualStrategy, StatusSnapshot


class DefaultExplainer:
    def explain_decision(self, decision: Decision) -> str:
        raise NotImplementedError

    def summarize_state(self, status: StatusSnapshot) -> str:
        raise NotImplementedError

    def parse_strategy_from_text(self, text: str, default_symbol: Optional[str] = None) -> Union[ManualStrategy, AutoConfig]:
        raise NotImplementedError

    def answer(self, question: str, status: StatusSnapshot) -> str:
        raise NotImplementedError

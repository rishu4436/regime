"""Bot 5 - DefaultExplainer: Grok-backed explanations with a deterministic template fallback.

Implements `backend.contracts.Explainer`. This module has no exchange client and never places orders:
it only turns contract models into text, and text into (validated) strategy configs.

Configuration (env): XAI_API_KEY (enables Grok), XAI_MODEL (default grok-4.7), XAI_BASE_URL
(default https://api.x.ai/v1), XAI_TIMEOUT_S (default 20), LLM_DISABLED=1 (force templates).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Sequence, TypeVar, Union

from backend.contracts import Alert, AutoConfig, Decision, ManualStrategy, PerformanceSummary, StatusSnapshot

from . import prompts, templates
from .client import GrokClient, GrokConfig, LLMError
from .parser import StrategyValidationError, keyword_parse, validate_llm_strategy

log = logging.getLogger(__name__)
T = TypeVar("T")

MAX_TEXT_CHARS = 2000
SOURCE_GROK = "grok"
SOURCE_TEMPLATE = "template"


def _clean(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1].rstrip() + "…"


class DefaultExplainer:
    """`Explainer` implementation. Pass `client=` to inject a (fake) GrokClient; default reads the env.

    `last_source` is "grok" or "template" for the most recent call (handy for logs / UI badges).
    """

    def __init__(self, client: Optional[GrokClient] = None, *, use_llm: Optional[bool] = None) -> None:
        self.client = client if client is not None else GrokClient(GrokConfig.from_env())
        self.use_llm = self.client.enabled if use_llm is None else (use_llm and self.client.enabled)
        self.last_source: str = SOURCE_TEMPLATE
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ internals
    def _try_llm(self, fn: Callable[[], T], fallback: Callable[[], T]) -> T:
        if self.use_llm:
            try:
                out = fn()
                self.last_source, self.last_error = SOURCE_GROK, None
                return out
            except (LLMError, StrategyValidationError) as e:
                self.last_error = str(e)
                log.warning("LLM path failed, using template fallback: %s", e)
            except Exception as e:  # noqa: BLE001 - the explainer must never break the trading loop
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Unexpected LLM error, using template fallback")
        self.last_source = SOURCE_TEMPLATE
        return fallback()

    def _chat_text(self, messages: list[dict[str, str]]) -> str:
        out = _clean(self.client.chat(messages))
        if not out:
            raise LLMError("empty response")
        return out

    # ------------------------------------------------------------------ Explainer protocol
    def explain_decision(self, decision: Decision) -> str:
        return self._try_llm(lambda: self._chat_text(prompts.explain_messages(decision)),
                             lambda: templates.explain_decision(decision))

    # `performance` / `alerts` are optional keyword-only extras (protocol-compatible): StatusSnapshot does not carry
    # PerformanceSummary or Alert in contracts v0.2.0, so the app passes them alongside when it has them.
    def summarize_state(self, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
                        alerts: Optional[Sequence[Alert]] = None) -> str:
        return self._try_llm(lambda: self._chat_text(prompts.summary_messages(status, performance, alerts)),
                             lambda: templates.summarize_state(status, performance, alerts))

    def parse_strategy_from_text(self, text: str, default_symbol: Optional[str] = None) -> Union[ManualStrategy, AutoConfig]:
        def via_llm() -> Union[ManualStrategy, AutoConfig]:
            msgs = prompts.parse_messages(text, default_symbol, ManualStrategy.model_json_schema(), AutoConfig.model_json_schema())
            raw = self.client.chat(msgs, temperature=0.0, max_tokens=1500)
            return validate_llm_strategy(raw, text, default_symbol)

        return self._try_llm(via_llm, lambda: keyword_parse(text, default_symbol))

    def answer(self, question: str, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
               alerts: Optional[Sequence[Alert]] = None) -> str:
        return self._try_llm(lambda: self._chat_text(prompts.answer_messages(question, status, performance, alerts)),
                             lambda: templates.answer(question, status, performance, alerts))

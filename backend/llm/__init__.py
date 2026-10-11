"""Bot 5 - LLM & Explanation Layer (Grok). Implements backend.contracts.Explainer. Never executes trades."""
from .client import GrokClient, GrokConfig, LLMError
from .explainer import DefaultExplainer
from .parser import StrategyValidationError, keyword_parse, validate_llm_strategy

__all__ = ["DefaultExplainer", "GrokClient", "GrokConfig", "LLMError", "StrategyValidationError", "keyword_parse", "validate_llm_strategy"]

"""Minimal xAI Grok client (OpenAI-compatible chat completions).

Reads configuration from the environment; never logs or returns the API key. Any transport or protocol
problem is raised as `LLMError` so callers can fall back to deterministic templates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://api.x.ai/v1"
# docs.x.ai/docs/models (Oct 2026) recommends grok-4.7 for chat. Override with XAI_MODEL (e.g. grok-4.3 for lower cost).
DEFAULT_MODEL = "grok-4.7"
DEFAULT_TIMEOUT_S = 20.0


class LLMError(RuntimeError):
    """Raised for any failure talking to the LLM (missing key, HTTP error, timeout, malformed response)."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class GrokConfig:
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = DEFAULT_TIMEOUT_S
    temperature: float = 0.2
    max_tokens: int = 800

    @classmethod
    def from_env(cls) -> "GrokConfig":
        """XAI_API_KEY, XAI_MODEL, XAI_BASE_URL, XAI_TIMEOUT_S. LLM_DISABLED=1 forces the template fallback."""
        disabled = os.environ.get("LLM_DISABLED", "").strip().lower() in {"1", "true", "yes"}
        key = (os.environ.get("XAI_API_KEY") or "").strip() or None
        return cls(
            api_key=None if disabled else key,
            model=(os.environ.get("XAI_MODEL") or "").strip() or DEFAULT_MODEL,
            base_url=((os.environ.get("XAI_BASE_URL") or "").strip() or DEFAULT_BASE_URL).rstrip("/"),
            timeout_s=_env_float("XAI_TIMEOUT_S", DEFAULT_TIMEOUT_S),
        )

    def __repr__(self) -> str:  # never leak the key
        return f"GrokConfig(model={self.model!r}, base_url={self.base_url!r}, timeout_s={self.timeout_s}, api_key={'***' if self.api_key else None})"


@dataclass
class GrokClient:
    """Thin wrapper over POST {base_url}/chat/completions. Inject `http` (an httpx.Client) in tests."""

    config: GrokConfig = field(default_factory=GrokConfig.from_env)
    http: Optional[httpx.Client] = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(self, messages: list[dict[str, str]], *, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
        if not self.enabled:
            raise LLMError("XAI_API_KEY not set")
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        url = f"{self.config.base_url}/chat/completions"
        try:
            if self.http is not None:
                resp = self.http.post(url, json=payload, headers=headers, timeout=self.config.timeout_s)
            else:
                with httpx.Client(timeout=self.config.timeout_s) as c:
                    resp = c.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMError(f"timeout after {self.config.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise LLMError(f"transport error: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError("malformed response") from e
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty response")
        return content.strip()

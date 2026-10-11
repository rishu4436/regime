"""Agent card / ERC-8004 registration metadata for Regime, the trading agent.

Two views of the same facts:

* :func:`build_registration_file` -> the kwargs we hand to the bnbagent SDK
  (``ERC8004Agent.generate_agent_uri(name=, description=, endpoints=)``), which
  turns them into an EIP-8004 ``registration-v1`` file stored on-chain as a
  base64 ``data:`` URI. Kept small because every byte is on-chain gas.
* :func:`build_agent_card` -> a richer, JSON-serialisable card for our own API /
  dashboard / README (adds capabilities, safety notes and the identity, if any).

Pure functions, no network, no SDK import.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from .config import (
    PUBLIC_BASE_URL_ENV,
    REPO_URL,
    STATUS_PATH,
    STATUS_URL_PLACEHOLDER,
    STUDIO_VERSION,
    TRADING_WALLET_ENV,
)

#: Product name. Also the ERC-8004 registration name and the name ``register.py`` looks up to avoid a double
#: registration (nothing is registered yet, so the identity carries this name from the start).
AGENT_NAME = "Regime"
AGENT_DISPLAY_NAME = "Regime"
#: On-chain description (kept short: every byte of the registration file is gas).
AGENT_DESCRIPTION = "Autonomous tokenized-stock trading agent on BNB Chain"
#: Public image for the ERC-8004 registration file. The SDK writes an empty image when none is passed.
AGENT_IMAGE_URL = "https://raw.githubusercontent.com/rishu4436/regime/main/docs/screenshots/dashboard.png"
REGISTRATION_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
#: Longer off-chain summary for the API / dashboard card only.
AGENT_SUMMARY = (
    "Regime trades 24/7 on its own on BNB Chain. It trades bStock and Ondo "
    "tokenized US equities through the Binance Agentic Wallet (spot, BSC mainnet), "
    "picks one strategy per market regime, enforces layered risk limits (agent caps "
    "plus wallet guardrails) and explains every decision in plain English with Grok."
)

CAPABILITIES: list[str] = [
    "tokenized-stock-spot-trading",
    "market-regime-detection",
    "strategy-selection",  # DCA, Smart DCA, Buy the Dip, Trend/Momentum, Grid
    "risk-controls",  # caps, stop-loss, trailing stop, cooldown, emergency stop
    "agentic-wallet-execution",  # Binance Agentic Wallet (baw CLI)
    "llm-decision-explanations",  # Grok, with template fallback
    "backtesting",
    "read-only-status",
]

SAFETY_NOTES: list[str] = [
    "Identity key is a separate small EVM key; it never holds or moves trading funds.",
    "Trading keys stay inside the Binance Agentic Wallet (MPC); spend limits are set in the Binance App.",
    "The status endpoint is read-only; it cannot place, cancel or modify orders.",
]


def _clean_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"URL must start with http:// or https://: {url!r}")
    return url.rstrip("/")


def resolve_status_url(status_url: str | None = None) -> str | None:
    """Explicit status URL, else ``$AGENT_PUBLIC_BASE_URL`` + ``/api/system``, else None."""
    explicit = _clean_url(status_url)
    if explicit:
        return explicit
    base = _clean_url(os.environ.get(PUBLIC_BASE_URL_ENV))
    return f"{base}{STATUS_PATH}" if base else None


def build_endpoints(status_url: str | None = None) -> list[dict[str, Any]]:
    """Service entries for the registration file (EIP-8004 ``services``).

    The repo link is always present (the SDK requires at least one endpoint and
    it is real and public). The read-only status endpoint is only included when
    a real public URL is configured, so we never put a placeholder on-chain.
    """
    endpoints: list[dict[str, Any]] = [{"name": "web", "endpoint": REPO_URL}]
    resolved = resolve_status_url(status_url)
    if resolved:
        endpoints.append(
            {
                "name": "status",
                "endpoint": resolved,
                "version": "1",
                "capabilities": ["read-only-status"],
            }
        )
    return endpoints


def build_registration_file(status_url: str | None = None) -> dict[str, Any]:
    """kwargs for ``ERC8004Agent.generate_agent_uri`` (endpoints as plain dicts)."""
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "endpoints": build_endpoints(status_url),
    }


def complete_registration_file(file: dict[str, Any]) -> dict[str, Any]:
    """Fill the EIP-8004 registration fields the bnbagent 0.5.0 generator leaves out.

    ``x402Support`` stays false: Regime does not charge callers via x402. ``active``
    stays true. An image, type, or either flag already set on ``file`` is kept.
    """
    out = dict(file)
    out.setdefault("type", REGISTRATION_TYPE)
    if not out.get("image"):
        out["image"] = AGENT_IMAGE_URL
    out.setdefault("x402Support", False)
    out.setdefault("active", True)
    return out


def registration_data_uri(file: dict[str, Any]) -> str:
    raw = json.dumps(file, sort_keys=True, separators=(",", ":")).encode()
    return "data:application/json;base64," + base64.b64encode(raw).decode()


def ensure_registration_uri(uri: str) -> str:
    """Re-encode a data-URI registration file with the fields ``complete_registration_file`` adds.

    A URI that is not a decodable registration JSON object is returned unchanged.
    """
    payload = uri.split(",", 1)[1] if uri.startswith("data:") and "," in uri else uri
    try:
        file = json.loads(base64.b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return uri
    if not isinstance(file, dict):
        return uri
    return registration_data_uri(complete_registration_file(file))


def build_metadata(trading_wallet: str | None = None) -> list[dict[str, str]]:
    """Extra on-chain metadata entries for ``register_agent(metadata=...)``.

    The SDK adds its own ``built_with`` entry. We deliberately avoid the
    ERC-8004 reserved ``agentWallet`` key (it needs a signed proof) and use
    ``trading_wallet`` for the Agentic Wallet's *public* address instead.
    """
    items = [
        {"key": "app", "value": "Regime"},
        {"key": "capabilities", "value": ",".join(CAPABILITIES)},
    ]
    wallet = (trading_wallet or os.environ.get(TRADING_WALLET_ENV) or "").strip()
    if wallet:
        if not (wallet.startswith("0x") and len(wallet) == 42):
            raise ValueError(
                f"{TRADING_WALLET_ENV} must be a 0x-prefixed 20-byte address"
            )
        items.append({"key": "trading_wallet", "value": wallet})
    return items


def build_agent_card(
    identity: dict[str, Any] | None = None,
    status_url: str | None = None,
) -> dict[str, Any]:
    """Human/API-facing agent card. ``identity`` is ``get_agent_identity()``'s output."""
    resolved = resolve_status_url(status_url)
    return {
        "name": AGENT_NAME,
        "display_name": AGENT_DISPLAY_NAME,
        "description": AGENT_DESCRIPTION,
        "summary": AGENT_SUMMARY,
        "capabilities": list(CAPABILITIES),
        "endpoints": {
            "repo": REPO_URL,
            "status": resolved or STATUS_URL_PLACEHOLDER,
            "status_configured": resolved is not None,
        },
        "chain": "BNB Smart Chain",
        "execution": "Binance Agentic Wallet (baw)",
        "explanations": "Grok (template fallback)",
        "safety": list(SAFETY_NOTES),
        "studio_version": STUDIO_VERSION,
        "identity": identity,
    }

"""Read-only access to our registered ERC-8004 agent identity.

``get_agent_identity()`` is what the API (``/api/system`` -> ``agent_identity``)
calls. It only reads the local JSON written by ``register.py``: no SDK, no RPC,
no key, never raises. Returns None when we are not registered (or the file is
unreadable / malformed), so the UI can simply hide the badge.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import STUDIO_VERSION, default_identity_path, network_info

logger = logging.getLogger(__name__)

PUBLIC_FIELDS = ("erc8004_id", "registry", "studio_version", "network", "explorer_url")


def load_identity_record(path: str | Path | None = None) -> dict[str, Any] | None:
    """Full local record (includes tx hash, owner address, ...) or None."""
    p = Path(path) if path else default_identity_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("agent identity file %s unreadable: %s", p, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def get_agent_identity(path: str | Path | None = None) -> dict[str, Any] | None:
    """``{erc8004_id, registry, studio_version, network, explorer_url}`` or None."""
    data = load_identity_record(path)
    if not data:
        return None
    raw_id = data.get("erc8004_id")
    registry = data.get("registry")
    if raw_id is None or not isinstance(registry, str) or not registry.startswith("0x"):
        return None
    try:
        erc8004_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if erc8004_id < 0:
        return None
    net = network_info(data.get("network") or "") or network_info(
        data.get("chain_id") or -1
    )
    explorer_url = data.get("explorer_url") or (
        net.agent_url(erc8004_id) if net else None
    )
    return {
        "erc8004_id": erc8004_id,
        "registry": registry,
        "studio_version": str(data.get("studio_version") or STUDIO_VERSION),
        "network": net.cli_name if net else str(data.get("network") or "unknown"),
        "explorer_url": explorer_url,
    }

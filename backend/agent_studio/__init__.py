"""BNB Agent Studio integration (ERC-8004 agent identity) for Regime, the trading agent.

Read-only, dependency-free API surface (safe to import from the FastAPI app):

    from backend.agent_studio import get_agent_identity, build_agent_card

Registration is a separate one-off CLI that needs ``bnbagent`` 0.5.0:

    python -m backend.agent_studio.register --dry-run
"""

from .agent_card import AGENT_NAME, build_agent_card, build_registration_file
from .config import NETWORKS, STUDIO_VERSION, NetworkInfo, default_identity_path
from .identity import get_agent_identity, load_identity_record

__all__ = [
    "AGENT_NAME",
    "NETWORKS",
    "STUDIO_VERSION",
    "NetworkInfo",
    "build_agent_card",
    "build_registration_file",
    "default_identity_path",
    "get_agent_identity",
    "load_identity_record",
]

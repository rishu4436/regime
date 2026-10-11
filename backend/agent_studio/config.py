"""Static configuration for the Agent Studio / ERC-8004 integration.

Registry addresses are the presets shipped in ``bnbagent`` 0.5.0
(``bnbagent.config.NETWORKS[...].registry_contract``). They are duplicated here
so the read-only API path never needs the SDK installed; a test asserts they
match the SDK when it is available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: The bnbagent SDK version this integration was written and verified against.
STUDIO_VERSION = "bnbagent-0.5.0"
SDK_PACKAGE = "bnbagent"
SDK_VERSION = "0.5.0"

#: Env var holding the *identity owner* key. A separate small hot key, never the
#: trading wallet (the trading wallet is the Binance Agentic Wallet / MPC and has
#: no exportable key anyway).
PRIVATE_KEY_ENV = "AGENT_STUDIO_PRIVATE_KEY"
#: Optional public base URL of our FastAPI (e.g. a tunnel); used for the
#: read-only status endpoint advertised in the agent card.
PUBLIC_BASE_URL_ENV = "AGENT_PUBLIC_BASE_URL"
#: Optional public address of the Agentic Wallet, stored as on-chain metadata
#: so judges can link the identity to the trading wallet. Address only.
TRADING_WALLET_ENV = "AGENT_TRADING_WALLET_ADDRESS"
#: Override for where the identity JSON lives.
IDENTITY_FILE_ENV = "AGENT_STUDIO_IDENTITY_FILE"

REPO_URL = "https://github.com/rishu4436/regime"
#: Placeholder until Rishu exposes the API publicly; .invalid never resolves.
STATUS_URL_PLACEHOLDER = "https://regime-agent.invalid/api/system"
STATUS_PATH = "/api/system"


@dataclass(frozen=True)
class NetworkInfo:
    cli_name: str  # testnet | mainnet
    sdk_name: str  # bnbagent network preset name
    chain_id: int
    registry: str
    explorer_agent_base: str  # 8004scan agent page prefix
    tx_explorer_base: str  # bscscan tx prefix

    def agent_url(self, erc8004_id: int | str) -> str:
        return f"{self.explorer_agent_base}/{erc8004_id}"

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.tx_explorer_base}/{tx_hash}"


NETWORKS: dict[str, NetworkInfo] = {
    "testnet": NetworkInfo(
        cli_name="testnet",
        sdk_name="bsc-testnet",
        chain_id=97,
        registry="0x8004A818BFB912233c491871b3d84c89A494BD9e",
        explorer_agent_base="https://8004scan.io/agents/bsc-testnet",
        tx_explorer_base="https://testnet.bscscan.com/tx",
    ),
    "mainnet": NetworkInfo(
        cli_name="mainnet",
        sdk_name="bsc-mainnet",
        chain_id=56,
        registry="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        explorer_agent_base="https://8004scan.io/agents/bsc",
        tx_explorer_base="https://bscscan.com/tx",
    ),
}
_BY_CHAIN = {n.chain_id: n for n in NETWORKS.values()}
_BY_SDK = {n.sdk_name: n for n in NETWORKS.values()}


def network_info(name: str | int) -> NetworkInfo | None:
    """Look up by CLI name (testnet/mainnet), SDK name (bsc-testnet) or chain id."""
    if isinstance(name, int):
        return _BY_CHAIN.get(name)
    key = str(name).strip().lower()
    return NETWORKS.get(key) or _BY_SDK.get(key)


def default_identity_path() -> Path:
    """Where register.py writes and get_agent_identity() reads (gitignored)."""
    override = os.environ.get(IDENTITY_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "agent_identity.json"

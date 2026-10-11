"""Pin our assumptions to the real bnbagent 0.5.0 API (skipped if not installed)."""

import inspect

import pytest

from backend.agent_studio.config import NETWORKS

bnbagent = pytest.importorskip("bnbagent")


def test_version_and_registry_presets_match_sdk():
    from bnbagent.config import NETWORKS as SDK_NETWORKS

    assert bnbagent.__version__ == "0.5.0"
    for net in NETWORKS.values():
        sdk = SDK_NETWORKS[net.sdk_name]
        assert sdk.chain_id == net.chain_id
        assert sdk.registry_contract.lower() == net.registry.lower()


def test_signatures_we_call():
    from bnbagent import AgentEndpoint, ERC8004Agent, EVMWalletProvider

    assert {"password", "private_key", "persist"} <= set(
        inspect.signature(EVMWalletProvider).parameters
    )
    assert {"wallet_provider", "network"} <= set(
        inspect.signature(ERC8004Agent).parameters
    )
    assert list(inspect.signature(ERC8004Agent.register_agent).parameters)[1:] == [
        "agent_uri",
        "metadata",
    ]
    assert {"name", "description", "endpoints"} <= set(
        inspect.signature(ERC8004Agent.generate_agent_uri).parameters
    )
    assert hasattr(ERC8004Agent, "get_local_agent_info") and hasattr(
        AgentEndpoint, "from_dict"
    )


def test_real_uri_generator_offline():
    from bnbagent.erc8004.agent_uri import AgentURIGenerator

    from backend.agent_studio.agent_card import build_registration_file

    reg = build_registration_file()
    uri = AgentURIGenerator.generate_agent_uri(
        **reg, identity_registry=NETWORKS["testnet"].registry, chain_id=97
    )
    data = AgentURIGenerator.decode_registration_file_from_base64(uri)
    assert (
        data["name"] == "Regime" and data["services"][0]["name"] == "web"
    )


def test_real_wallet_in_memory_no_keystore(tmp_path):
    from bnbagent import EVMWalletProvider

    w = EVMWalletProvider(
        password="x", private_key="0x" + "11" * 32, persist=False, wallets_dir=tmp_path
    )
    assert w.address.startswith("0x") and not list(tmp_path.iterdir())

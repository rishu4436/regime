import json

import pytest

from backend.agent_studio import build_agent_card, get_agent_identity
from backend.agent_studio.agent_card import (
    AGENT_NAME,
    build_endpoints,
    build_metadata,
    build_registration_file,
)
from backend.agent_studio.config import NETWORKS, STATUS_URL_PLACEHOLDER, STUDIO_VERSION


def _write(tmp_path, data):
    p = tmp_path / "agent_identity.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return p


def test_none_when_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STUDIO_IDENTITY_FILE", str(tmp_path / "missing.json"))
    assert get_agent_identity() is None


def test_reads_identity_and_builds_explorer_url(tmp_path):
    p = _write(
        tmp_path,
        {
            "erc8004_id": "1234",
            "registry": NETWORKS["mainnet"].registry,
            "tx_hash": "0xabc",
            "network": "mainnet",
            "studio_version": STUDIO_VERSION,
            "owner_address": "0x1",
        },
    )
    ident = get_agent_identity(p)
    assert ident == {
        "erc8004_id": 1234,
        "registry": NETWORKS["mainnet"].registry,
        "studio_version": STUDIO_VERSION,
        "network": "mainnet",
        "explorer_url": "https://8004scan.io/agents/bsc/1234",
    }
    # only public fields leak to the API
    assert "tx_hash" not in ident and "owner_address" not in ident


def test_testnet_explorer_and_chain_id_fallback(tmp_path):
    p = _write(tmp_path, {"erc8004_id": 7, "registry": "0xreg", "chain_id": 97})
    ident = get_agent_identity(p)
    assert ident["network"] == "testnet"
    assert ident["explorer_url"] == "https://8004scan.io/agents/bsc-testnet/7"
    assert ident["studio_version"] == STUDIO_VERSION


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[]",
        {"registry": "0xabc"},
        {"erc8004_id": 1},
        {"erc8004_id": "x", "registry": "0xabc"},
        {"erc8004_id": -1, "registry": "0xabc"},
        {"erc8004_id": 1, "registry": "nope"},
    ],
)
def test_malformed_files_return_none(tmp_path, payload):
    assert get_agent_identity(_write(tmp_path, payload)) is None


def test_registration_file_never_has_placeholder(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLIC_BASE_URL", raising=False)
    reg = build_registration_file()
    assert reg["name"] == AGENT_NAME
    assert reg["name"] == "Regime"
    assert reg["description"] == "Autonomous tokenized-stock trading agent on BNB Chain"
    assert [e["name"] for e in reg["endpoints"]] == ["web"]
    assert all(STATUS_URL_PLACEHOLDER not in e["endpoint"] for e in reg["endpoints"])


def test_status_endpoint_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLIC_BASE_URL", "https://abc.trycloudflare.com/")
    eps = build_endpoints()
    assert eps[1]["endpoint"] == "https://abc.trycloudflare.com/api/system"
    assert eps[1]["capabilities"] == ["read-only-status"]
    with pytest.raises(ValueError):
        build_endpoints("ftp://bad")


def test_metadata_trading_wallet(monkeypatch):
    monkeypatch.delenv("AGENT_TRADING_WALLET_ADDRESS", raising=False)
    keys = [m["key"] for m in build_metadata()]
    assert keys == ["app", "capabilities"] and "agentWallet" not in keys
    addr = "0x" + "ab" * 20
    assert build_metadata(addr)[-1] == {"key": "trading_wallet", "value": addr}
    with pytest.raises(ValueError):
        build_metadata("0x123")


def test_agent_card_shape(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLIC_BASE_URL", raising=False)
    card = build_agent_card(identity=None)
    assert card["endpoints"]["status"] == STATUS_URL_PLACEHOLDER
    assert card["endpoints"]["status_configured"] is False
    assert "read-only-status" in card["capabilities"]
    assert card["identity"] is None
    json.dumps(card)  # serialisable


def test_agent_card_is_named_regime():
    from backend.agent_studio.agent_card import build_agent_card

    card = build_agent_card()
    assert card["name"] == card["display_name"] == "Regime"
    assert card["description"] == "Autonomous tokenized-stock trading agent on BNB Chain"
    assert "Agentic Wallet" in card["summary"] and "Grok" in card["summary"]
    assert card["endpoints"]["repo"] == "https://github.com/rishu4436/regime"

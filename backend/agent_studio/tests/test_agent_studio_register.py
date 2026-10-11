"""Offline tests for register.py with a fake bnbagent SDK (no chain, no network)."""

import json
from types import SimpleNamespace

import pytest

from backend.agent_studio import get_agent_identity
from backend.agent_studio import register as reg
from backend.agent_studio.config import NETWORKS

KEY = "0x" + "11" * 32
OWNER = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"  # address of KEY


class PartialErr(Exception):
    def __init__(self, agent_id, tx_hash=None, agent_uri="data:x"):
        super().__init__("partial")
        self.agent_id, self.tx_hash, self.agent_uri = agent_id, tx_hash, agent_uri


class PendingErr(Exception):
    def __init__(self, tx_hash):
        super().__init__("pending")
        self.tx_hash = tx_hash


def make_sdk(calls, *, existing=None, register_result=None, register_exc=None):
    class Wallet:
        def __init__(self, password, private_key=None, persist=True, **kw):
            assert password and private_key == KEY and persist is False
            calls["wallet"] = True
            self.address = OWNER

    class Agent:
        def __init__(self, wallet_provider, network="bsc-testnet", debug=False):
            calls["network"] = network
            self.contract_address = {
                "bsc-testnet": NETWORKS["testnet"].registry,
                "bsc-mainnet": NETWORKS["mainnet"].registry,
            }[network]

        def get_local_agent_info(self, name):
            calls["lookup"] = name
            return existing

        def generate_agent_uri(self, name, description, endpoints, **kw):
            calls["uri_args"] = (name, description, [e.to_dict() for e in endpoints])
            return "data:application/json;base64,e30="

        def register_agent(self, agent_uri, metadata=None):
            calls["register"] = (agent_uri, metadata)
            if register_exc:
                raise register_exc
            return register_result or {
                "success": True,
                "transactionHash": bytes.fromhex("ab" * 32),
                "agentId": 42,
                "agentURI": "data:final",
            }

    class Endpoint:
        def __init__(self, d):
            self.d = d

        @classmethod
        def from_dict(cls, d):
            return cls(d)

        def to_dict(self):
            return self.d

    class Account:
        @staticmethod
        def from_key(k):
            if k != KEY:
                raise ValueError(f"bad key {k}")
            return SimpleNamespace(address=OWNER)

    class Gen:
        @staticmethod
        def generate_registration_file(**kw):
            return {
                "name": kw["name"],
                "services": kw["endpoints"],
                "registrations": [],
            }

        @staticmethod
        def generate_agent_uri(**kw):
            return "data:application/json;base64," + "A" * 100

    return SimpleNamespace(
        version="0.5.0",
        AgentEndpoint=Endpoint,
        ERC8004Agent=Agent,
        EVMWalletProvider=Wallet,
        PartialError=PartialErr,
        PendingError=PendingErr,
        AgentURIGenerator=Gen,
        Account=Account,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in (
        "AGENT_STUDIO_PRIVATE_KEY",
        "AGENT_PUBLIC_BASE_URL",
        "AGENT_TRADING_WALLET_ADDRESS",
    ):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "agent_identity.json"
    monkeypatch.setenv("AGENT_STUDIO_IDENTITY_FILE", str(path))
    calls = {}
    return SimpleNamespace(path=path, calls=calls, mp=monkeypatch, out=[])


def run(env, argv, sdk=None):
    env.mp.setattr(reg, "_load_sdk", lambda: sdk or make_sdk(env.calls))
    return reg.main(argv, out=env.out.append)


def test_parser_defaults_to_testnet_and_has_no_key_flag():
    p = reg.build_parser()
    assert p.parse_args([]).network == "testnet"
    assert all("key" not in a.dest for a in p._actions)


def test_dry_run_sends_nothing_and_writes_nothing(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    assert run(env, ["--dry-run"]) == 0
    plan = json.loads(env.out[0])
    assert plan["dry_run"] and plan["network"] == "testnet" and plan["chain_id"] == 97
    assert plan["registry"] == NETWORKS["testnet"].registry
    assert plan["owner_address"] == OWNER
    assert plan["agent_uri_bytes"] > 0
    assert "register" not in env.calls and "wallet" not in env.calls
    assert not env.path.exists()
    assert KEY not in "\n".join(env.out) and KEY[2:] not in "\n".join(env.out)


def test_dry_run_mainnet_without_key(env):
    assert run(env, ["--dry-run", "--network", "mainnet"]) == 0
    plan = json.loads(env.out[0])
    assert (
        plan["registry"] == NETWORKS["mainnet"].registry
        and "set $" in plan["owner_address"]
    )


def test_real_run_requires_env_key(env):
    assert run(env, []) == reg.EXIT_USAGE
    assert "AGENT_STUDIO_PRIVATE_KEY" in env.out[-1]
    assert "register" not in env.calls


def test_invalid_key_is_not_echoed(env):
    bad = "0x" + "zz" * 32
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", bad)
    assert run(env, []) == reg.EXIT_USAGE
    assert bad not in "\n".join(env.out)


def test_mainnet_requires_confirm_flag(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    assert run(env, ["--network", "mainnet"]) == reg.EXIT_USAGE
    assert "--confirm-mainnet" in env.out[-1]
    assert "register" not in env.calls


def test_refuses_trading_wallet_key(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    env.mp.setenv("AGENT_TRADING_WALLET_ADDRESS", OWNER.lower())
    assert run(env, []) == reg.EXIT_USAGE
    assert "trading wallet" in env.out[-1]


def test_testnet_registration_writes_identity(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    assert run(env, []) == 0
    assert env.calls["network"] == "bsc-testnet"
    assert env.calls["lookup"] == "Regime"
    name, _desc, eps = env.calls["uri_args"]
    assert name == "Regime" and eps[0]["name"] == "web"
    _, metadata = env.calls["register"]
    assert {"key": "app", "value": "Regime"} in metadata
    rec = json.loads(env.path.read_text())
    assert rec["erc8004_id"] == 42 and rec["tx_hash"] == "0x" + "ab" * 32
    assert (
        rec["network"] == "testnet" and rec["registry"] == NETWORKS["testnet"].registry
    )
    assert rec["studio_version"] == "bnbagent-0.5.0" and rec["status"] == "registered"
    assert rec["tx_url"] == "https://testnet.bscscan.com/tx/0x" + "ab" * 32
    assert KEY[2:] not in env.path.read_text()
    assert get_agent_identity()["erc8004_id"] == 42


def test_mainnet_with_confirm(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    assert run(env, ["--network", "mainnet", "--confirm-mainnet"]) == 0
    assert env.calls["network"] == "bsc-mainnet"
    assert get_agent_identity()["explorer_url"] == "https://8004scan.io/agents/bsc/42"


def test_existing_file_is_not_overwritten(env):
    env.path.write_text(
        json.dumps({"erc8004_id": 5, "registry": "0xr", "network": "testnet"})
    )
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    assert run(env, []) == 0
    assert "Already registered" in env.out[-1]
    assert (
        "register" not in env.calls
        and json.loads(env.path.read_text())["erc8004_id"] == 5
    )


def test_recovers_agent_already_on_chain(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    sdk = make_sdk(
        env.calls,
        existing={
            "agent_id": 77,
            "agent_uri": "data:old",
            "name": "Regime",
        },
    )
    assert run(env, [], sdk=sdk) == 0
    assert "register" not in env.calls
    rec = json.loads(env.path.read_text())
    assert rec["erc8004_id"] == 77 and rec["status"] == "recovered_existing"


def test_partial_registration_still_saves_id(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    sdk = make_sdk(env.calls, register_exc=PartialErr(agent_id=9, tx_hash="0xdead"))
    assert run(env, [], sdk=sdk) == reg.EXIT_PARTIAL
    rec = json.loads(env.path.read_text())
    assert rec["erc8004_id"] == 9 and rec["status"] == "registered_uri_update_pending"


def test_pending_tx_writes_nothing(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    sdk = make_sdk(env.calls, register_exc=PendingErr("0xbeef"))
    assert run(env, [], sdk=sdk) == reg.EXIT_PENDING
    assert not env.path.exists() and "0xbeef" in env.out[-1]


def test_sdk_failure_is_reported(env):
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    sdk = make_sdk(env.calls, register_exc=RuntimeError("insufficient funds for gas"))
    assert run(env, [], sdk=sdk) == reg.EXIT_FAILED
    assert "insufficient funds" in env.out[-1] and not env.path.exists()

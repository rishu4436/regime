"""Offline tests for the localhost Agent Studio API (fake RPC, fake key library, fake SDK)."""

import json
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent_studio import api
from backend.agent_studio import register as reg
from backend.agent_studio.tests.test_agent_studio_register import (
    KEY,
    OWNER,
    PendingErr,
    make_sdk,
)

NEW_KEY = "0x" + "22" * 32
NEW_ADDR = "0x1563915e194D8CfBA1943570603F7606A3115508"


class FakeAccount:
    created = 0

    @staticmethod
    def create():
        FakeAccount.created += 1
        return SimpleNamespace(key=bytes.fromhex(NEW_KEY[2:]), address=NEW_ADDR)

    @staticmethod
    def from_key(k):
        addr = {KEY: OWNER, NEW_KEY: NEW_ADDR}.get(k)
        if addr is None:
            raise ValueError(f"bad key {k}")
        return SimpleNamespace(address=addr)


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k in (
        "AGENT_STUDIO_PRIVATE_KEY",
        "AGENT_STUDIO_NETWORK",
        "AGENT_TRADING_WALLET_ADDRESS",
        "AGENT_PUBLIC_BASE_URL",
        "RPC_URL",
        "RPC_URL_BSC_MAINNET",
        "RPC_URL_BSC_TESTNET",
    ):
        monkeypatch.delenv(k, raising=False)
    ident = tmp_path / "agent_identity.json"
    keyf = tmp_path / "logs" / "agent_studio_identity.key"
    monkeypatch.setenv("AGENT_STUDIO_IDENTITY_FILE", str(ident))
    monkeypatch.setenv(api.KEY_FILE_ENV, str(keyf))
    monkeypatch.setattr(api, "_load_account_cls", lambda: FakeAccount)
    state = SimpleNamespace(balance=0.0, rpc_calls=[], calls={}, ident=ident, keyf=keyf, mp=monkeypatch)

    def fake_rpc(url, address):
        state.rpc_calls.append((url, address))
        if state.balance is None:
            raise OSError("offline")
        return state.balance

    monkeypatch.setattr(api, "_rpc_get_balance", fake_rpc)
    monkeypatch.setattr(reg, "_load_sdk", lambda: make_sdk(state.calls))
    api._balance_cache.clear()
    app = FastAPI()
    app.include_router(api.router)
    state.client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    state.app = app
    return state


def test_module_imports_without_sdk_or_eth_account():
    # The app imports this module at startup; it must not need bnbagent/eth_account.
    src = Path(api.__file__).read_text()
    head = src.split("# ── helpers")[0]
    assert "import bnbagent" not in head and "from eth_account" not in head


def test_non_local_requests_get_403(s):
    remote = TestClient(s.app, base_url="http://localhost", client=("192.168.1.5", 50000))
    assert remote.get("/api/agent-studio/status").status_code == 403
    r = s.client.get("/api/agent-studio/status", headers={"x-forwarded-for": "8.8.8.8"})
    assert r.status_code == 403 and "localhost" in r.json()["detail"]
    lan = TestClient(s.app, base_url="http://192.168.1.2:8000", client=("127.0.0.1", 1))
    assert lan.post("/api/agent-studio/create-key").status_code == 403
    r = s.client.post("/api/agent-studio/register", headers={"origin": "http://192.168.1.2:5173"},
                      json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 403
    assert not s.keyf.exists() and "register" not in s.calls


def test_status_before_anything(s):
    r = s.client.get("/api/agent-studio/status")
    assert r.status_code == 200
    assert r.json() == {
        "registered": False,
        "network": "mainnet",
        "identity_address": None,
        "bnb_balance": None,
        "bnb_needed": 0.002,
        "erc8004_id": None,
        "explorer_url": None,
        "has_key": False,
    }
    assert s.rpc_calls == []  # no address, no RPC


def test_create_key_writes_private_file_and_never_returns_key(s, caplog):
    caplog.set_level("DEBUG")
    r = s.client.post("/api/agent-studio/create-key")
    assert r.status_code == 200 and r.json() == {"identity_address": NEW_ADDR}
    assert s.keyf.read_text().strip() == NEW_KEY
    if os.name == "posix":
        assert stat.S_IMODE(s.keyf.stat().st_mode) == 0o600
    st = s.client.get("/api/agent-studio/status").json()
    assert st["has_key"] and st["identity_address"] == NEW_ADDR and st["bnb_balance"] == 0.0
    everything = r.text + json.dumps(st) + caplog.text
    assert NEW_KEY[2:] not in everything


def test_create_key_refuses_to_overwrite(s):
    assert s.client.post("/api/agent-studio/create-key").status_code == 200
    before = s.keyf.read_text()
    r = s.client.post("/api/agent-studio/create-key")
    assert r.status_code == 409 and "overwrite" in r.json()["detail"]
    assert s.keyf.read_text() == before


def test_env_key_counts_as_key_and_blocks_create(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    st = s.client.get("/api/agent-studio/status").json()
    assert st["has_key"] and st["identity_address"] == OWNER
    assert s.client.post("/api/agent-studio/create-key").status_code == 409
    assert not s.keyf.exists()


def test_create_key_without_eth_account_is_503(s):
    def missing():
        raise api.HTTPException(503, "The eth-account package isn't installed")

    s.mp.setattr(api, "_load_account_cls", missing)
    r = s.client.post("/api/agent-studio/create-key")
    assert r.status_code == 503 and "eth-account" in r.json()["detail"]
    assert not s.keyf.exists()
    # status still works and never 500s
    assert s.client.get("/api/agent-studio/status").status_code == 200


def test_balance_failure_is_null_not_500(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = None
    r = s.client.get("/api/agent-studio/status")
    assert r.status_code == 200 and r.json()["bnb_balance"] is None


def test_balance_rpc_uses_sdk_env_override(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.mp.setenv("RPC_URL_BSC_MAINNET", "http://rpc.example.invalid")
    s.client.get("/api/agent-studio/status")
    assert s.rpc_calls == [("http://rpc.example.invalid", OWNER)]


def test_register_requires_mainnet_confirmation(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 1.0
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet"})
    assert r.status_code == 400 and "confirmation" in r.json()["detail"]
    r = s.client.post("/api/agent-studio/register", json={"network": "BSC Mainnet", "confirm_mainnet": False})
    assert r.status_code == 400
    assert "register" not in s.calls


def test_register_requires_key(s):
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 400 and "Create the identity wallet" in r.json()["detail"]


def test_register_unknown_network(s):
    r = s.client.post("/api/agent-studio/register", json={"network": "solana", "confirm_mainnet": True})
    assert r.status_code == 400


def test_register_needs_enough_bnb(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.0005
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    d = r.json()["detail"]
    assert r.status_code == 400 and OWNER in d and "0.002" in d and "0.0005" in d
    assert "register" not in s.calls


def test_register_balance_unreadable_is_503(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = None
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 503 and "register" not in s.calls


def test_register_without_sdk_is_503(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 1.0

    def no_sdk():
        raise reg.RegistrationError("bnbagent is not installed")

    s.mp.setattr(reg, "_load_sdk", no_sdk)
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 503 and "bnbagent" in r.json()["detail"]


def test_register_happy_path_with_key_file(s, caplog):
    caplog.set_level("DEBUG")
    s.mp.setattr(api, "_load_account_cls", lambda: FakeAccount)
    assert s.client.post("/api/agent-studio/create-key").status_code == 200
    s.balance = 0.0025

    seen = {}
    real_register = reg.register

    def spy(args, net, out=print, key=None):
        seen["key_ok"] = key == NEW_KEY
        return real_register(args, net, out=out, key=KEY)  # fake SDK wallet expects KEY

    s.mp.setattr(reg, "register", spy)
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "erc8004_id": 42,
        "explorer_url": "https://8004scan.io/agents/bsc/42",
        "tx_hash": "0x" + "ab" * 32,
    }
    assert seen["key_ok"] and s.calls["network"] == "bsc-mainnet"
    rec = json.loads(s.ident.read_text())
    assert rec["network"] == "mainnet" and rec["chain_id"] == 56 and rec["status"] == "registered"
    st = s.client.get("/api/agent-studio/status").json()
    assert st["registered"] and st["erc8004_id"] == 42 and st["explorer_url"].endswith("/42")
    assert st["network"] == "mainnet"
    assert NEW_KEY[2:] not in r.text + caplog.text and KEY[2:] not in caplog.text


def test_register_env_key_and_already_registered_409(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.01
    ok = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert ok.status_code == 200
    s.calls.clear()
    again = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert again.status_code == 409 and "already registered" in again.json()["detail"]
    assert "register" not in s.calls


def test_register_testnet_and_network_names(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.01
    r = s.client.post("/api/agent-studio/register", json={"network": "bsc-testnet"})
    assert r.status_code == 200 and "bsc-testnet" in r.json()["explorer_url"]
    assert s.calls["network"] == "bsc-testnet"
    assert s.client.get("/api/agent-studio/status").json()["network"] == "testnet"


def test_register_pending_is_504_and_writes_nothing(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.01
    s.mp.setattr(reg, "_load_sdk", lambda: make_sdk(s.calls, register_exc=PendingErr("0x" + "cd" * 32)))
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 504 and "cd" * 32 in r.json()["detail"]
    assert not s.ident.exists()


def test_register_sdk_failure_is_502_without_key(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.01
    s.mp.setattr(reg, "_load_sdk", lambda: make_sdk(s.calls, register_exc=RuntimeError("rpc down")))
    r = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 502 and "rpc down" in r.json()["detail"] and KEY[2:] not in r.text


def test_double_click_registers_once(s):
    s.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    s.balance = 0.01
    gate, entered = threading.Event(), threading.Event()
    real_register = reg.register

    def slow(*a, **kw):
        entered.set()
        gate.wait(5)
        return real_register(*a, **kw)

    s.mp.setattr(reg, "register", slow)
    results = []
    t = threading.Thread(target=lambda: results.append(
        s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})))
    t.start()
    assert entered.wait(5)
    second = s.client.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    gate.set()
    t.join(10)
    assert second.status_code == 409 and "in progress" in second.json()["detail"]
    assert results[0].status_code == 200


def test_real_eth_account_key_roundtrip_if_installed(s):
    pytest.importorskip("eth_account")
    s.mp.setattr(api, "_load_account_cls", _real_loader)
    r = s.client.post("/api/agent-studio/create-key")
    assert r.status_code == 200
    from eth_account import Account

    assert Account.from_key(s.keyf.read_text().strip()).address == r.json()["identity_address"]


def _real_loader():
    from eth_account import Account

    return Account


def test_key_file_path_is_gitignored():
    """The default key file must never show up in git status (needs a git checkout)."""
    root = api.repo_root()
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    rel = api.KEY_FILE_RELATIVE
    chk = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=root)
    assert chk.returncode == 0, f"{rel} is not gitignored"
    gi = (root / ".gitignore").read_text()
    assert rel in gi.splitlines()
    target = root / rel
    if target.exists():
        created = False
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not-a-key (test placeholder)\n")
        created = True
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        assert "agent_studio_identity.key" not in out
    finally:
        if created:
            target.unlink()

"""Agent Studio (/api/agent-studio/*) through the real Regime app (create_app), SIM only.

Fake RPC, fake key library and fake bnbagent SDK only: nothing here touches BSC mainnet or testnet.
"""
from __future__ import annotations

import builtins
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agent_studio import api
from backend.agent_studio import register as reg
from backend.agent_studio.tests.test_agent_studio_register import KEY, OWNER, make_sdk
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.mocks import MockExchangeClient

REPO = Path(__file__).resolve().parents[2]
NEW_KEY = "0x" + "22" * 32
NEW_ADDR = "0x1563915e194D8CfBA1943570603F7606A3115508"
STATUS = "/api/agent-studio/status"


class FakeAccount:
    @staticmethod
    def create():
        return SimpleNamespace(key=bytes.fromhex(NEW_KEY[2:]), address=NEW_ADDR)

    @staticmethod
    def from_key(k):
        addr = {KEY: OWNER, NEW_KEY: NEW_ADDR}.get(k)
        if addr is None:
            raise ValueError(f"bad key {k}")
        return SimpleNamespace(address=addr)


def _app(tmp_path):
    s = Settings(sim_seed=1234, log_dir=str(tmp_path / "applogs"), state_dir=str(tmp_path / "applogs"))
    return create_app(s, build_components(s, exchange=MockExchangeClient(seed=1234, n_candles=s.sim_replay_bars)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("AGENT_STUDIO_PRIVATE_KEY", "AGENT_STUDIO_NETWORK", "RPC_URL", "RPC_URL_BSC_MAINNET", "RPC_URL_BSC_TESTNET"):
        monkeypatch.delenv(k, raising=False)
    st = SimpleNamespace(balance=0.0, rpc=[], calls={}, mp=monkeypatch, tmp=tmp_path)
    st.ident = tmp_path / "agent_identity.json"
    st.keyf = tmp_path / "logs" / "agent_studio_identity.key"
    monkeypatch.setenv("AGENT_STUDIO_IDENTITY_FILE", str(st.ident))
    monkeypatch.setenv(api.KEY_FILE_ENV, str(st.keyf))

    def fake_rpc(url, address):  # never a real network call
        st.rpc.append((url, address))
        return st.balance

    monkeypatch.setattr(api, "_rpc_get_balance", fake_rpc)
    monkeypatch.setattr(reg, "_load_sdk", lambda: make_sdk(st.calls))
    api._balance_cache.clear()
    st.app = _app(tmp_path)
    st.local = TestClient(st.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    return st


def _no_libs(monkeypatch):
    """Make eth_account and bnbagent unimportable, as on a PC without the studio extra."""
    real = builtins.__import__

    def imp(name, *a, **kw):
        if name.split(".")[0] in ("eth_account", "bnbagent"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **kw)

    for m in [m for m in sys.modules if m.split(".")[0] in ("eth_account", "bnbagent")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setattr(builtins, "__import__", imp)


def test_router_is_mounted_in_the_app(env):
    paths = set(env.app.openapi()["paths"])
    assert {STATUS, "/api/agent-studio/create-key", "/api/agent-studio/register"} <= paths
    # a localhost request reaches the router (not a 404 from the app)
    assert env.local.get(STATUS).status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("get", STATUS, None),
    ("post", "/api/agent-studio/create-key", None),
    ("post", "/api/agent-studio/register", {"network": "mainnet", "confirm_mainnet": True}),
])
def test_non_localhost_gets_403(env, method, path, body):
    kw = {"json": body} if body is not None else {}
    remote = TestClient(env.app, base_url="http://localhost", client=("192.168.1.5", 50000))
    assert getattr(remote, method)(path, **kw).status_code == 403
    # spoofed proxy hop, foreign Host and foreign Origin are refused too
    assert getattr(env.local, method)(path, headers={"x-forwarded-for": "8.8.8.8"}, **kw).status_code == 403
    assert getattr(TestClient(env.app, base_url="http://regime.example.com", client=("127.0.0.1", 1)), method)(path, **kw).status_code == 403
    assert getattr(env.local, method)(path, headers={"origin": "https://evil.example"}, **kw).status_code == 403
    assert not env.keyf.exists() and "register" not in env.calls and not env.rpc
    # the rest of the app is unaffected by the guard
    assert remote.get("/health").status_code == 200


def test_status_works_without_eth_account_or_bnbagent(env):
    _no_libs(env.mp)
    r = env.local.get(STATUS)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["registered"] is False and b["has_key"] is False and b["identity_address"] is None
    assert b["network"] == "mainnet" and b["bnb_needed"] == 0.002 and b["bnb_balance"] is None
    assert not env.rpc  # no address -> no RPC call at all
    # with a key file but no eth_account: still 200, just no address
    env.keyf.parent.mkdir(parents=True)
    env.keyf.write_text(NEW_KEY + "\n")
    b = env.local.get(STATUS).json()
    assert b["has_key"] is True and b["identity_address"] is None
    # create-key explains what to install instead of crashing
    env.keyf.unlink()
    r = env.local.post("/api/agent-studio/create-key")
    assert r.status_code == 503 and "pip install" in r.json()["detail"]
    # the rest of the app still runs
    assert env.local.get("/health").json()["trading_mode"] == "SIM"


@pytest.mark.parametrize("body", [{}, {"network": "mainnet"}, {"network": "mainnet", "confirm_mainnet": False}, {"network": "56"}])
def test_register_refuses_without_confirm_mainnet(env, body):
    env.mp.setattr(api, "_load_account_cls", lambda: FakeAccount)
    env.mp.setenv("AGENT_STUDIO_PRIVATE_KEY", KEY)
    env.balance = 1.0
    r = env.local.post("/api/agent-studio/register", json=body)
    assert r.status_code == 400 and "confirmation" in r.json()["detail"]
    assert env.calls == {} and not env.ident.exists() and not env.rpc


def test_key_never_in_responses_logs_or_git_status(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.mp.setattr(api, "_load_account_cls", lambda: FakeAccount)
    texts = []
    texts.append(env.local.get(STATUS).text)
    r = env.local.post("/api/agent-studio/create-key")
    assert r.status_code == 200 and r.json() == {"identity_address": NEW_ADDR}
    texts.append(r.text)
    assert env.keyf.read_text().strip() == NEW_KEY
    if sys.platform != "win32":
        assert (env.keyf.stat().st_mode & 0o777) == 0o600
    texts.append(env.local.post("/api/agent-studio/create-key").text)  # 409, no overwrite
    assert env.keyf.read_text().strip() == NEW_KEY
    texts.append(env.local.get(STATUS).text)
    env.balance = 0.0001
    texts.append(env.local.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True}).text)
    env.balance = 0.01
    real = reg.register
    env.mp.setattr(reg, "register", lambda args, net, out=print, key=None: real(args, net, out=out, key=KEY))  # fake SDK wants KEY
    r = env.local.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True})
    assert r.status_code == 200, r.text
    assert r.json()["erc8004_id"] == 42 and env.calls["network"] == "bsc-mainnet"
    texts.append(r.text)
    texts.append(env.local.get(STATUS).text)
    texts.append(env.local.get("/api/system").text)
    texts.append(env.local.get("/openapi.json").text)
    # a failing SDK error path must not leak it either
    env.ident.unlink()
    env.mp.setattr(reg, "register", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rpc down")))
    texts.append(env.local.post("/api/agent-studio/register", json={"network": "mainnet", "confirm_mainnet": True}).text)

    logs = caplog.text + "".join(p.read_text(errors="ignore") for p in (env.tmp / "applogs").rglob("*") if p.is_file())
    for secret in (NEW_KEY, NEW_KEY[2:], KEY[2:]):
        for t in texts:
            assert secret not in t
        assert secret not in logs


def test_key_file_is_gitignored(tmp_path, env):
    # the default location is logs/agent_studio_identity.key in the repo, and git ignores it
    env.mp.delenv(api.KEY_FILE_ENV)
    assert api.key_file_path() == REPO / "logs" / "agent_studio_identity.key"
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    chk = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "logs/agent_studio_identity.key"])
    assert chk.returncode == 0
    # end to end in a scratch repo with our .gitignore: create the key through the app, git status stays clean
    scratch = tmp_path / "repo"
    scratch.mkdir()
    shutil.copy(REPO / ".gitignore", scratch / ".gitignore")
    subprocess.run(["git", "init", "-q", str(scratch)], check=True)
    env.mp.setenv(api.KEY_FILE_ENV, str(scratch / "logs" / "agent_studio_identity.key"))
    env.mp.setattr(api, "_load_account_cls", lambda: FakeAccount)
    assert env.local.post("/api/agent-studio/create-key").status_code == 200
    assert (scratch / "logs" / "agent_studio_identity.key").is_file()
    out = subprocess.run(["git", "-C", str(scratch), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    assert "agent_studio_identity" not in out and NEW_KEY[2:] not in out
    assert out.strip() == "?? .gitignore"

"""v0.8 dashboard "Connect wallet": POST/GET /api/wallet/connect + POST /api/wallet/reconcile.

Always runs against the bundled fake baw (backend/app/dev_baw.py, a real subprocess speaking baw's JSON) - never the
real baw, never the network. Covers: localhost-only (403 otherwise, incl. a forwarded non-local client), the exact
field sets the dashboard reads, pending -> connected, wallet + limits re-read after connect, a different wallet
blocking LIVE ("different wallet connected", no orders - not even exits) until reconcile, reconcile dropping positions
the new wallet doesn't hold, and expired / failed sign-ins.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from backend.app import main as appmain
from backend.contracts import OrderSide, OrderStatus

from .helpers import make_client

LOCAL = ("127.0.0.1", 50123)
FAKE = [sys.executable, os.path.join(os.path.dirname(appmain.__file__), "dev_baw.py")]
START_KEYS = {"session_id", "qr_url", "pairing_code", "expires_at"}
POLL_KEYS = {"status", "address", "wallet_changed", "error"}
OLD, NEW = "0x2222222222222222222222222222222222222222", "0x3333333333333333333333333333333333333333"


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    for k in ("BINANCE_WALLET_ADDRESS", "BAW_CMD", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_BAW_VERIFY_S", "0.3")
    monkeypatch.setenv("FAKE_BAW_OUTCOME", "ok")


def _local(c: TestClient) -> TestClient:
    return TestClient(c.app, client=LOCAL, base_url="http://localhost:8000")


def _connect(c: TestClient, timeout: float = 15.0) -> tuple[dict, list[dict]]:
    r = c.post("/api/wallet/connect")
    assert r.status_code == 200, r.text
    start = r.json()
    seen = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = c.get("/api/wallet/connect", params={"session_id": start["session_id"]})
        assert p.status_code == 200, p.text
        seen.append(p.json())
        if p.json()["status"] != "pending":
            break
        time.sleep(0.1)
    return start, seen


# ---------------------------------------------------------------- SIM (dashboard demo path)
def test_non_local_client_gets_403_and_never_starts_baw():
    c = make_client()  # TestClient host is "testclient", not localhost
    r = c.post("/api/wallet/connect")
    assert r.status_code == 403 and "only works on the PC running" in r.json()["detail"]
    assert c.get("/api/wallet/connect", params={"session_id": "x"}).status_code == 403
    assert c.post("/api/wallet/reconcile").status_code == 403
    assert c.app.state.st.wallet_connect is None  # nothing built, nothing spawned
    lan = TestClient(c.app, client=("192.168.1.20", 4000))
    assert lan.post("/api/wallet/connect").status_code == 403
    proxied = _local(c)  # e.g. a proxy on localhost forwarding a LAN client
    assert proxied.post("/api/wallet/connect", headers={"X-Forwarded-For": "192.168.1.20"}).status_code == 403
    assert proxied.post("/api/wallet/connect", headers={"Host": "192.168.1.20:8000"}).status_code == 403
    # Vite proxy on 0.0.0.0 (UI_HOST): local connection, Host rewritten to localhost, but the page came from the LAN
    assert proxied.post("/api/wallet/connect", headers={"Origin": "http://192.168.1.20:5173"}).status_code == 403
    assert proxied.post("/api/wallet/connect", headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert proxied.post("/api/wallet/connect", headers={"Host": "127.0.0.1:5173"}).status_code == 200


def test_sim_pending_then_connected_with_exact_dashboard_fields():
    c = _local(make_client())
    w0 = c.get("/api/wallet").json()["updated_at"]
    start, seen = _connect(c)
    assert set(start) == START_KEYS and start["qr_url"].startswith("https://") and start["pairing_code"]
    assert start["expires_at"].endswith("Z")
    assert seen[0]["status"] == "pending", seen
    final = seen[-1]
    assert set(final) == POLL_KEYS
    assert final["status"] == "connected" and final["address"].startswith("0x") and final["wallet_changed"] is False
    assert final["error"] is None
    w = c.get("/api/wallet").json()
    assert w["updated_at"] > w0 and w["address"] == final["address"]  # SIM shows the signed-in address
    assert "different wallet connected" not in c.get("/api/live/status").json()["blocked_reasons"]
    assert c.app.state.st.loop.state.get("wallet_address") == final["address"]
    # one process-wide instance
    wc = c.app.state.st.wallet_connect
    _connect(c)
    assert c.app.state.st.wallet_connect is wc


@pytest.mark.parametrize("outcome,status", [("expired", "expired"), ("failed", "failed")])
def test_expired_and_failed_sign_in(monkeypatch, outcome, status):
    monkeypatch.setenv("FAKE_BAW_OUTCOME", outcome)
    c = _local(make_client())
    _, seen = _connect(c)
    assert seen[-1]["status"] == status and seen[-1]["error"] and seen[-1]["address"] is None
    assert c.app.state.st.loop.state.get("wallet_address") is None


def test_qr_timeout_reports_expired():
    c = _local(make_client())
    os.environ["FAKE_BAW_VERIFY_S"] = "30"
    try:
        r = c.post("/api/wallet/connect").json()
        wc = c.app.state.st.wallet_connect
        wc._sessions[r["session_id"]]["expires"] = time.time() - 1  # QR TTL passed while verify still waits
        p = c.get("/api/wallet/connect", params={"session_id": r["session_id"]}).json()
        assert p["status"] == "expired" and "expired" in p["error"]
    finally:
        os.environ["FAKE_BAW_VERIFY_S"] = "0.3"


def test_unknown_session_is_failed_not_500():
    c = _local(make_client())
    p = c.get("/api/wallet/connect", params={"session_id": "nope"}).json()
    assert p["status"] == "failed" and p["error"]


# ---------------------------------------------------------------- LIVE (LiveSim, no real money, fake baw only)
@pytest.fixture
def live(monkeypatch):
    from . import test_small_wallet_live as sw
    if sw.SimClient is None:
        pytest.skip("real engine / risk / SIM client not importable")
    real_run = subprocess.run

    def guarded(argv, *a, **k):  # only the fake baw may run
        assert list(argv)[:2] == FAKE, f"unexpected subprocess {argv}"
        return real_run(argv, *a, **k)
    c, ex = sw._client(100, sw.WEEKDAY, monkeypatch)
    monkeypatch.setattr(subprocess, "run", guarded)
    st = c.app.state.st
    from backend.exchange.baw import BawCli
    from backend.exchange.wallet_connect import WalletConnect
    st.wallet_connect = WalletConnect(cli=BawCli(cmd=FAKE))
    return _local(c), ex, sw


def _buy_once(c, ex, sw) -> None:
    r = c.post("/api/manual/activate", json={"strategy": sw._buy_strategy("NVDAB")})
    assert r.status_code == 200, r.text
    for _ in range(4):
        c.post("/api/loop/tick")
        ex.advance()
        if c.get("/api/portfolio").json()["positions"]:
            return
    pytest.fail("no BUY filled in LIVE sim")


def test_live_connect_rereads_wallet_and_limits(live, monkeypatch):
    c, ex, _ = live
    monkeypatch.setenv("FAKE_BAW_ADDRESS", OLD)
    calls = {"n": 0}
    orig = ex.get_wallet_settings
    ex.get_wallet_settings = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    c.get("/api/wallet")
    w0, n0 = c.get("/api/wallet").json()["updated_at"], calls["n"]
    _, seen = _connect(c)
    assert seen[-1]["status"] == "connected" and seen[-1]["wallet_changed"] is False
    assert calls["n"] > n0  # wallet settings (daily quota / limits) fetched again
    w = c.get("/api/wallet").json()
    assert w["updated_at"] > w0 and w["limits"]["per_order_cap_usdt"]
    assert c.get("/api/live/status").json()["can_trade"] is True


def test_live_different_wallet_blocks_until_reconciled(live, monkeypatch):
    c, ex, sw = live
    monkeypatch.setenv("FAKE_BAW_ADDRESS", OLD)
    assert _connect(c)[1][-1]["wallet_changed"] is False  # first sign-in becomes the saved wallet
    _buy_once(c, ex, sw)
    n_exec = len(ex.executions)
    monkeypatch.setenv("FAKE_BAW_ADDRESS", NEW)
    final = _connect(c)[1][-1]
    assert final == {"status": "connected", "address": NEW, "wallet_changed": True, "error": None}
    ls = c.get("/api/live/status").json()
    assert ls["can_trade"] is False and "different wallet connected" in ls["blocked_reasons"]
    assert ls["wallet_block"]["address"] == NEW and ls["wallet_block"]["expected"] == OLD
    assert "different wallet connected" in c.get("/api/system").json()["live"]["blocked_reasons"]
    # crash the price so the stop-loss would fire: still no order of any kind
    ex._price_override["NVDAB"] = 1.0
    for _ in range(3):
        ds = c.post("/api/loop/tick").json()
        ex.advance()
        assert all(d["action"] == "STAY_OUT" and "different wallet connected" in d["reason"] for d in ds), ds
    assert len(ex.executions) == n_exec
    ex._price_override.pop("NVDAB")
    # the new wallet doesn't hold the saved NVDAB position
    monkeypatch.setattr(ex, "get_wallet", lambda: {"address": NEW, "balances": {"USDT": 80.0, "BNB": 0.05}})
    r = c.post("/api/wallet/reconcile")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["was_blocked"] and not rec["wallet_blocked"] and rec["address"] == NEW
    assert [d["symbol"] for d in rec["dropped"]] == ["NVDAB"] and rec["kept"] == []
    assert c.get("/api/portfolio").json()["positions"] == []
    ls = c.get("/api/live/status").json()
    assert ls["can_trade"] is True and ls["wallet_block"] is None
    st = c.app.state.st.loop.state
    assert st.get("wallet_address") == NEW and st.get("wallet_block") is None
    # reconnecting the same (now saved) wallet is not a change
    assert _connect(c)[1][-1]["wallet_changed"] is False


def test_live_reconcile_keeps_positions_the_wallet_holds(live, monkeypatch):
    c, ex, sw = live
    monkeypatch.setenv("FAKE_BAW_ADDRESS", OLD)
    _connect(c)
    _buy_once(c, ex, sw)
    monkeypatch.setenv("FAKE_BAW_ADDRESS", NEW)
    assert _connect(c)[1][-1]["wallet_changed"] is True
    rec = c.post("/api/wallet/reconcile").json()  # LiveSim wallet still holds the NVDAB it bought
    assert [d["symbol"] for d in rec["kept"]] == ["NVDAB"] and rec["dropped"] == []
    assert len(c.get("/api/portfolio").json()["positions"]) == 1
    assert c.get("/api/live/status").json()["can_trade"] is True


def test_wallet_block_survives_restart(live, monkeypatch, tmp_path):
    c, ex, sw = live
    loop = c.app.state.st.loop
    loop.state.path = str(tmp_path / "agent_state.live.json")
    loop.state.update(wallet_address=OLD)
    monkeypatch.setenv("FAKE_BAW_ADDRESS", NEW)
    assert _connect(c)[1][-1]["wallet_changed"] is True
    from backend.app.state import AgentStateStore
    assert AgentStateStore(loop.state.path).get("wallet_block")["address"] == NEW
    loop.state = AgentStateStore(loop.state.path)
    loop._restore()
    assert loop.wallet_block and "different wallet connected" in loop.live_status()["blocked_reasons"]


def test_expected_address_falls_back_to_env(live, monkeypatch):
    c, ex, _ = live
    c.app.state.st.loop.s.wallet_address = OLD  # BINANCE_WALLET_ADDRESS
    monkeypatch.setenv("FAKE_BAW_ADDRESS", NEW)
    assert _connect(c)[1][-1]["wallet_changed"] is True

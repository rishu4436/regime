"""LIVE wallet session expiry end to end, with Bot 4's real BawExecutor/BawCli on a FAKE baw runner (no subprocess,
no network, no keys). Outage of 11 Oct 10:33: `baw wallet status` failed with "illegal parameter" while the backend
kept reporting wallet_connected / can_trade true. Now: session_expired everywhere, exits held, recovery clears it."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from backend.app.live import SESSION_EXPIRED_REASON
from backend.contracts import Candle
from backend.exchange.baw import BawCli, RunResult
from backend.exchange.baw_executor import BawExecutor, IntentStore

from .test_live import FakeLiveExchange, _buy, first_buy_tick, live_client

ADDR = "0x2222222222222222222222222222222222222222"


class FakeBaw:
    """`baw ... --json` envelopes. mode 'ok' = signed in; 'illegal' = every call fails with SERVICE_ERROR 2."""

    def __init__(self):
        self.mode = "ok"
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout_s):
        args = [a for a in argv if a not in ("baw", "--json")]
        self.calls.append(args)
        if self.mode == "illegal":
            return RunResult(1, json.dumps({"success": False, "error": {"code": 2, "name": "SERVICE_ERROR",
                                                                         "message": "illegal parameter"}}))
        key = args[:2]
        if key == ["wallet", "status"]:
            return RunResult(0, json.dumps({"success": True, "data": {"status": "CONNECTED"}}))
        if key == ["wallet", "address"]:
            return RunResult(0, json.dumps({"success": True, "data": {"addresses": [{"binanceChainId": "56", "address": ADDR}]}}))
        if key == ["wallet", "balance"]:
            return RunResult(0, json.dumps({"success": True, "data": [
                {"symbol": "USDT", "balance": "200", "value": "200", "price": "1"},
                {"symbol": "BNB", "balance": "0.05", "value": "30", "price": "600"}]}))
        if key == ["wallet", "settings"]:
            return RunResult(0, json.dumps({"success": True, "data": {"dailyLimit": "100", "quotaUsed": "0", "quotaLeft": "100",
                                                                        "tradeAllTokens": True, "abnormalTxnHandling": "AutoReject"}}))
        raise AssertionError(f"unexpected baw call {args}")


class BawWalletEx(FakeLiveExchange):
    """Mock market data + fills; wallet reads through Bot 4's BawExecutor (LiveWalletMixin shape). Counts swaps."""

    def __init__(self, tmp_path, **kw):
        super().__init__(**kw)
        self.baw = FakeBaw()
        self.executor = BawExecutor(cli=BawCli(runner=self.baw, cmd=["baw"]), intents=IntentStore(tmp_path / "intents.json"),
                                    env={}, wallet_ttl_s=0.0)
        self.get_balances = self.executor.get_balances
        self.swaps = 0

    def get_wallet(self):
        return dict(self.executor.get_wallet())

    def get_wallet_settings(self):
        return self.executor.get_wallet_settings()

    def execute_swap(self, order, quote, **kw):
        self.swaps += 1
        if self.baw.mode == "illegal":  # what a real swap would do with a dead session
            from backend.contracts import ExecutionResult, OrderStatus
            return ExecutionResult(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side,
                                   status=OrderStatus.REJECTED, path=order.path, mode=order.mode,
                                   error="[session_expired] preflight: baw wallet status: illegal parameter")
        return super().execute_swap(order, quote)


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess in test")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess in test")))


def _client(tmp_path):
    ex = BawWalletEx(tmp_path)
    c, ex = live_client(ex=ex, state_dir=str(tmp_path / "state"))
    return c, ex


def _scheduled_read(c):
    """What the WALLET_REFRESH_SEC schedule (tick / monitor) does when the read is due."""
    L = c.app.state.st.loop
    L.wcache["read_at"] = None
    assert L.wallet_refresh_if_due()


def test_illegal_parameter_status_gives_session_expired_banner_fields(tmp_path):
    c, ex = _client(tmp_path)
    ls = c.get("/api/live/status").json()
    assert ls["wallet_connected"] and ls["can_trade"] and not ls["session_expired"]
    assert c.get("/api/wallet").json()["session_expired"] is False

    ex.baw.mode = "illegal"  # the session silently dies
    n = len(ex.baw.calls)
    c.get("/api/wallet"), c.get("/api/live/status"), c.get("/api/system")
    assert len(ex.baw.calls) == n  # the dashboard endpoints serve the last read and never call baw
    _scheduled_read(c)
    w = c.get("/api/wallet").json()
    assert w["session_expired"] is True and w["connected"] is False and w["balances"] == []  # -> Reconnect button
    assert w["stale"] is False  # a session problem is a definitive read, not a network blip
    ls = c.get("/api/live/status").json()
    assert ls["session_expired"] is True and ls["wallet_connected"] is False and ls["can_trade"] is False
    assert SESSION_EXPIRED_REASON in ls["blocked_reasons"]  # matches the frontend SessionBanner regex (session + wallet)
    assert not any(r.startswith("Paused") for r in ls["blocked_reasons"])  # no duplicate session line
    assert c.get("/api/system").json()["live"]["can_trade"] is False
    al = [a for a in c.get("/api/alerts").json() if a["code"] == "WALLET_SESSION_EXPIRED"]
    assert len(al) == 1
    _scheduled_read(c)
    assert len([a for a in c.get("/api/alerts").json() if a["code"] == "WALLET_SESSION_EXPIRED"]) == 1  # once per outage


def test_recovery_after_successful_status_clears_it_and_resumes_only_session_pause(tmp_path):
    c, ex = _client(tmp_path)
    ex.baw.mode = "illegal"
    _scheduled_read(c)
    assert c.get("/api/live/status").json()["session_expired"]
    assert c.get("/api/control").json()["paused"]

    ex.baw.mode = "ok"  # user scanned the QR / clicked Reconnect wallet: the sign-in forces one fresh read
    c.app.state.st.loop.on_wallet_connected(ADDR, changed=False)
    ls = c.get("/api/live/status").json()
    assert ls["session_expired"] is False and ls["wallet_connected"] and ls["can_trade"] and ls["blocked_reasons"] == []
    w = c.get("/api/wallet").json()
    assert w["session_expired"] is False and w["connected"] is True
    assert not c.get("/api/control").json()["paused"]
    assert any(a["code"] == "WALLET_SESSION_RESTORED" for a in c.get("/api/alerts").json())

    # a pause the user set is never lifted by a wallet read
    c.post("/api/control/pause", json={"reason": "my pause"})
    _scheduled_read(c)
    assert c.get("/api/control").json()["paused"]


def test_tick_reads_wallet_and_blocks_buys_when_expired(tmp_path):
    c, ex = _client(tmp_path)
    ex.baw.mode = "illegal"
    ds = first_buy_tick(c, ex)  # the tick itself reads the wallet (no /api call needed)
    assert ex.swaps == 0
    assert all(d["action"] != "BUY" for d in ds)
    assert c.get("/api/live/status").json()["session_expired"]


def test_exit_on_expired_session_is_held_without_swaps_then_sells_after_reconnect(tmp_path):
    c, ex = _client(tmp_path)
    d = _buy(first_buy_tick(c, ex, sl=2.0))
    assert d["execution"]["status"] == "FILLED"
    swaps_after_buy = ex.swaps
    entry = ex.executions[-1].avg_price

    ex.baw.mode = "illegal"
    for i in range(3):  # price under the stop for several ticks while signed out
        last = ex.series[ex.cursor - 1]
        ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=entry * 0.97, high=entry * 0.975,
                       low=entry * 0.9, close=entry * 0.91, volume=last.volume))
        ds = c.post("/api/loop/tick").json()
        held = [x for x in ds if "exit held" in x["reason"]]
        assert held and held[0]["action"] == "STAY_OUT" and SESSION_EXPIRED_REASON in held[0]["reason"], ds
        assert not any(x["action"] == "SELL" for x in ds)
    assert ex.swaps == swaps_after_buy  # no failed swaps spammed
    assert len(c.get("/api/portfolio").json()["positions"]) == 1
    assert len([a for a in c.get("/api/alerts").json() if a["code"] == "EXIT_HELD_SESSION"]) == 1

    ex.baw.mode = "ok"  # signed in again: the first tick sells
    last = ex.series[ex.cursor - 1]
    ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=entry * 0.92, high=entry * 0.93,
                   low=entry * 0.9, close=entry * 0.91, volume=last.volume))
    ds = c.post("/api/loop/tick").json()
    sells = [x for x in ds if x["action"] == "SELL"]
    assert sells and sells[0]["execution"]["status"] == "FILLED" and sells[0]["reason"].startswith("STOP_LOSS")
    assert ex.swaps == swaps_after_buy + 1
    assert c.get("/api/portfolio").json()["positions"] == []


def test_exit_swap_failing_with_session_expired_marks_session_and_stops(tmp_path):
    """Session dies between the wallet read and the swap: one rejected swap, then exits are held."""
    c, ex = _client(tmp_path)
    _buy(first_buy_tick(c, ex, sl=2.0))
    entry = ex.executions[-1].avg_price
    n = ex.swaps

    def flip(order, quote, **kw):  # the tick's wallet read still succeeds, but the swap hits the dead session
        ex.baw.mode = "illegal"
        return BawWalletEx.execute_swap(ex, order, quote)
    ex.execute_swap = flip
    last = ex.series[ex.cursor - 1]
    ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=entry * 0.97, high=entry * 0.975,
                   low=entry * 0.9, close=entry * 0.91, volume=last.volume))
    ds = c.post("/api/loop/tick").json()
    assert ex.swaps == n + 1
    assert any("exit held" in x["reason"] for x in ds)
    assert c.get("/api/live/status").json()["session_expired"]
    ds = c.post("/api/loop/tick").json()
    assert ex.swaps == n + 1  # held: no second failing swap

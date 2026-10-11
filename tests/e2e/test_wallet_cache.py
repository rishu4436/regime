"""LIVE wallet cache (hotfix: fewer baw calls). Bot 4's real BawExecutor/BawCli on a FAKE baw runner: no subprocess,
no network, no keys.

baw wipes its own session when a token refresh fails during a network blip, and the dashboard polled /api/wallet and
/api/live/status every 4-5 s, each one running `baw wallet status/address/balance/settings`. Now:
- GET /api/wallet, /api/live/status and /api/system serve the LAST read (read_at / age) and never call baw;
- the loop re-reads every WALLET_REFRESH_SEC and right before every order (pass 2 of the BUY risk check, before exits);
- a network failure keeps the last good read (stale), blocks BUYs, lets exits out and backs off 60 / 120 / 300 s;
- 'illegal parameter' on `baw wallet status` is still an expired session.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from backend.app.live import SESSION_EXPIRED_REASON
from backend.contracts import Candle, utc_now
from backend.exchange.baw import RunResult

from .test_live import ContextRisk, _buy, first_buy_tick, live_client
from .test_session_expired import BawWalletEx, FakeBaw, _no_subprocess  # noqa: F401  (autouse fixture)

ADDR = "0x2222222222222222222222222222222222222222"
NET_ERRORS = {
    "getaddrinfo": lambda: RunResult(1, json.dumps({"success": False, "error": {
        "code": 99999999, "name": "REQUEST_ERROR", "message": "request failed: getaddrinfo ENOTFOUND web3.binance.com"}})),
    "connect_error": "raise",
}


class NetBaw(FakeBaw):
    """FakeBaw + mode 'net': every call fails the way a network blip does (error envelope or a raised ConnectError)."""

    def __init__(self, kind="getaddrinfo"):
        super().__init__()
        self.kind = kind

    def __call__(self, argv, timeout_s):
        if self.mode == "net":
            self.calls.append([a for a in argv if a not in ("baw", "--json")])
            if self.kind == "connect_error":
                raise ConnectionError("ConnectError: [Errno -3] Temporary failure in name resolution")
            return NET_ERRORS[self.kind]()
        return super().__call__(argv, timeout_s)


def _client(tmp_path, kind="getaddrinfo", risk=None):
    ex = BawWalletEx(tmp_path)
    ex.baw = NetBaw(kind)
    ex.executor.cli.runner = ex.baw
    c, ex = live_client(ex=ex, risk=risk, state_dir=str(tmp_path / "state"))
    return c, ex


def _L(c):
    return c.app.state.st.loop


def _due(c):
    """Pretend WALLET_REFRESH_SEC (or the current back-off) has passed."""
    L = _L(c)
    if L.wcache["retry_at"] is not None:
        L.wcache["retry_at"] = utc_now() - timedelta(seconds=1)
    else:
        L.wcache["read_at"] = utc_now() - timedelta(seconds=L.wallet_refresh_s + 1)


def _stop_bar(ex, entry):
    last = ex.series[ex.cursor - 1]
    ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=entry * 0.97, high=entry * 0.975,
                   low=entry * 0.9, close=entry * 0.91, volume=last.volume))


def test_dashboard_polling_and_ticks_make_a_bounded_number_of_baw_calls(tmp_path):
    c, ex = _client(tmp_path)
    startup = len(ex.baw.calls)
    assert 1 <= startup <= 5  # one startup read: status, address, balance, settings (+ settings for the limits)
    c.post("/api/manual/activate", json={"strategy": {**__import__("tests.e2e.test_live", fromlist=["buy_strategy"])
                                                      .buy_strategy(), "entry": {"logic": "AND", "rsi_below": {
                                                          "enabled": True, "threshold": 0.0001}}}})
    for _ in range(40):  # N ticks with no order
        c.post("/api/loop/tick")
        ex.advance()
        for _ in range(3):  # M dashboard polls per tick
            c.get("/api/wallet"), c.get("/api/live/status"), c.get("/api/system")
    assert len(ex.baw.calls) == startup, ex.baw.calls[startup:]  # nothing until WALLET_REFRESH_SEC
    w = c.get("/api/wallet").json()
    assert w["connected"] and w["read_at"] and w["age_s"] is not None and w["stale"] is False
    ls = c.get("/api/live/status").json()
    assert ls["wallet_read_at"] and ls["wallet_stale"] is False and ls["can_trade"]

    _due(c)  # the slow schedule: one read on the next tick, then quiet again
    c.post("/api/loop/tick"), c.post("/api/loop/tick")
    assert len(ex.baw.calls) == 2 * startup


def test_a_buy_reads_the_wallet_once_right_before_the_order(tmp_path):
    c, ex = _client(tmp_path)
    n = len(ex.baw.calls)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "BUY" and d["execution"]["status"] == "FILLED"
    calls = ex.baw.calls[n:]
    assert [x[:2] for x in calls].count(["wallet", "status"]) == 1, calls  # one fresh read (pass 2), not one per pass
    assert 3 <= len(calls) <= 5


def test_pre_order_read_feeds_the_limits_with_its_own_fetched_at(tmp_path):
    from backend.risk import WalletLimits
    risk = ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(
        trading_mode=__import__("backend.contracts", fromlist=["TradingMode"]).TradingMode.LIVE), live_allowed=True)
    c, ex = _client(tmp_path, risk=risk)
    L = _L(c)
    old = utc_now() - timedelta(minutes=5)
    L.wallet_settings._cached = WalletLimits.from_baw({"dailyLimit": "100", "quotaUsed": "0", "quotaLeft": "100"}, fetched_at=old)
    t0 = utc_now()
    d = _buy(first_buy_tick(c, ex))
    assert d["execution"]["status"] == "FILLED"
    with_quote = [ctx for ctx in risk.contexts if getattr(ctx, "quote", None) is not None]
    assert with_quote, "pass 2 (with the quote) never ran"
    wl = with_quote[-1].wallet_limits
    assert wl is not None and wl.fetched_at is not None
    fa = wl.fetched_at if wl.fetched_at.tzinfo else wl.fetched_at.replace(tzinfo=t0.tzinfo)
    assert fa >= t0 - timedelta(seconds=1), (fa, t0)  # the fresh read's own fetched_at, never the 5-minute-old copy
    assert fa != old


@pytest.mark.parametrize("kind", ["getaddrinfo", "connect_error"])
def test_network_error_is_not_session_expired_blocks_buys_lets_exits_out_and_backs_off(tmp_path, kind):
    c, ex = _client(tmp_path, kind=kind)
    d = _buy(first_buy_tick(c, ex, sl=2.0))
    assert d["execution"]["status"] == "FILLED"
    entry = ex.executions[-1].avg_price
    good = c.get("/api/wallet").json()

    ex.baw.mode = "net"
    _due(c)
    n0 = len(ex.baw.calls)
    c.post("/api/loop/tick")  # scheduled read fails
    L = _L(c)
    assert len(ex.baw.calls) > n0
    assert L.wallet_session["expired"] is False and not c.get("/api/control").json()["paused"]
    w = c.get("/api/wallet").json()
    assert w["stale"] is True and w["session_expired"] is False and w["read_error"]
    assert w["balances"] == good["balances"] and w["read_at"] and w["age_s"] >= L.wallet_refresh_s  # last good read kept
    ls = c.get("/api/live/status").json()
    assert ls["wallet_stale"] and not ls["session_expired"] and not ls["can_trade"]
    assert any("wallet read failed" in r for r in ls["blocked_reasons"])
    assert SESSION_EXPIRED_REASON not in ls["blocked_reasons"]
    assert len([a for a in c.get("/api/alerts").json() if a["code"] == "WALLET_READ_FAILED"]) == 1
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "WALLET_SESSION_EXPIRED"]
    first_delay = (L.wcache["retry_at"] - L.wcache["attempt_at"]).total_seconds()
    assert first_delay == pytest.approx(60, abs=1)

    # during the back-off: ticks with a BUY signal and dashboard polls never call baw, and no BUY goes out
    swaps = ex.swaps
    n1 = len(ex.baw.calls)
    for _ in range(5):
        ex.advance()
        ds = c.post("/api/loop/tick").json()
        c.get("/api/wallet"), c.get("/api/live/status")
        assert not any(x["action"] == "BUY" for x in ds)
    assert len(ex.baw.calls) == n1
    assert ex.swaps == swaps

    # a stop-loss still goes out (no wallet read during the back-off, no session hold)
    _stop_bar(ex, entry)
    ds = c.post("/api/loop/tick").json()
    sells = [x for x in ds if x["action"] == "SELL"]
    assert sells and sells[0]["execution"]["status"] == "FILLED" and sells[0]["reason"].startswith("STOP_LOSS"), ds
    assert ex.swaps == swaps + 1 and len(ex.baw.calls) == n1
    assert c.get("/api/portfolio").json()["positions"] == []

    # flat again, the strategy wants to BUY: blocked with a clear reason, still no baw call
    for _ in range(5):  # new bars past the 2-bar re-entry cooldown
        last = ex.series[ex.cursor - 1]
        ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=last.close, high=last.close * 1.001,
                       low=last.close * 0.999, close=last.close, volume=last.volume))
        ds = c.post("/api/loop/tick").json()
        assert not any(x["action"] == "BUY" for x in ds)
    blocked = [x for x in c.get("/api/decisions?limit=20").json() if "wallet read failed" in (x.get("reason") or "")]
    assert blocked, [x["reason"][:120] for x in c.get("/api/decisions?limit=8").json()]
    assert ex.swaps == swaps + 1 and len(ex.baw.calls) == n1

    # back-off grows 60 -> 120 -> 300 -> 300, one read attempt per expiry (never a tight loop)
    delays = [first_delay]
    for _ in range(3):
        _due(c)
        before = len(ex.baw.calls)
        c.post("/api/loop/tick"), c.post("/api/loop/tick")
        assert len(ex.baw.calls) - before >= 1
        delays.append((L.wcache["retry_at"] - L.wcache["attempt_at"]).total_seconds())
    assert [round(x) for x in delays] == [60, 120, 300, 300]
    assert L.wallet_session["expired"] is False

    # network back: the next read clears stale and BUYs are allowed again
    ex.baw.mode = "ok"
    _due(c)
    c.post("/api/loop/tick")
    w = c.get("/api/wallet").json()
    assert w["stale"] is False and w["read_error"] is None and w["connected"]
    assert c.get("/api/live/status").json()["can_trade"]
    assert any(a["code"] == "WALLET_READ_OK" for a in c.get("/api/alerts").json())


def test_network_error_at_the_pre_order_read_blocks_that_buy(tmp_path):
    c, ex = _client(tmp_path)
    real = ex.get_quote

    def quote_then_blip(*a, **k):  # the network drops between pass 1 and the pre-order read
        ex.baw.mode = "net"
        return real(*a, **k)
    ex.get_quote = quote_then_blip
    ds = first_buy_tick(c, ex)
    assert ex.swaps == 0 and not any(x["action"] == "BUY" for x in ds)
    assert any("wallet read failed" in x["reason"] for x in ds), [x["reason"] for x in ds]
    assert _L(c).wallet_session["expired"] is False


def test_illegal_parameter_still_means_session_expired(tmp_path):
    c, ex = _client(tmp_path)
    ex.baw.mode = "illegal"
    _due(c)
    c.post("/api/manual/activate", json={"strategy": __import__("tests.e2e.test_live", fromlist=["buy_strategy"]).buy_strategy()})
    c.post("/api/loop/tick")
    ls = c.get("/api/live/status").json()
    assert ls["session_expired"] and not ls["wallet_stale"] and not ls["can_trade"]
    assert SESSION_EXPIRED_REASON in ls["blocked_reasons"]
    assert c.get("/api/control").json()["paused"]
    assert ex.swaps == 0


def test_connect_and_reconcile_force_one_fresh_read_even_during_back_off(tmp_path):
    c, ex = _client(tmp_path)
    ex.baw.mode = "net"
    _due(c)
    _L(c).wallet_refresh_if_due()
    assert _L(c).wcache["stale"]
    ex.baw.mode = "ok"
    n = len(ex.baw.calls)
    _L(c).on_wallet_connected(ADDR, changed=False)
    assert [x[:2] for x in ex.baw.calls[n:]].count(["wallet", "status"]) == 1
    assert not _L(c).wcache["stale"]
    n = len(ex.baw.calls)
    _L(c).reconcile_wallet()  # POST /api/wallet/reconcile (localhost only)
    assert [x[:2] for x in ex.baw.calls[n:]].count(["wallet", "status"]) == 1

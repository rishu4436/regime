"""LIVE flow with mocks (no network, no real money): wallet view, explorer links, BUYs always need the user,
pre-trade checks (Bot 3 check_live duck-typed + app hard caps), exits stay automatic, allow_live in /api/system."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import json

from backend.app.fallbacks import FallbackRiskManager
from backend.app.live import WalletSettingsProvider, app_live_checks, enrich_payload, explorer_url
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import (
    Action, AgentMode, Candle, ExecutionResult, ExecutionPath, OrderSide, OrderStatus, Portfolio, Quote, Signal,
    Timeframe, TradingMode, utc_now,
)
from backend.mocks import MockExchangeClient

from .helpers import SEED, make_client, tick_until, trading_strategy


def _has_bot3_live() -> bool:
    try:
        from backend.risk import LiveContext  # noqa: F401
        return True
    except Exception:
        return False

SYM = "AAPLB"


class FakeLiveExchange(MockExchangeClient):
    """Mock data + fills, reporting mode LIVE and real-looking BSC tx hashes. `advance()` releases the next bar
    (PollingFeed only acts on a new bar); `push()` appends a custom bar."""

    def __init__(self, balances=None, wallet=True, **kw):
        super().__init__(seed=SEED, mode=TradingMode.PAPER, **kw)
        self._mode = TradingMode.LIVE
        self.series = super().get_candles(SYM, Timeframe.H1, limit=self._n)
        self.cursor = 210
        self.balances = {"BNB": 0.05, "USDT": 200.0} if balances is None else balances
        self.wallet_address = "0x1111111111111111111111111111111111111111"
        self.quote_shift = 0.0
        self.expired_quotes = False
        if not wallet:
            self.get_balances = None  # no balance source
        self.quotes_asked = 0

    def get_balances(self):  # noqa: D401 - Bot 4 to provide (duck-typed)
        return dict(self.balances)

    def advance(self, n=1):
        self.cursor += n

    def push(self, candle: Candle):
        self.series = self.series[:self.cursor] + [candle]
        self.cursor = len(self.series)

    def get_candles(self, symbol, timeframe=Timeframe.H1, limit=200):
        return self.series[:self.cursor][-limit:]

    def get_price(self, symbol):
        return self.series[self.cursor - 1].close

    def get_quote(self, symbol, side, notional, path=ExecutionPath.SWAP):
        self.quotes_asked += 1
        q = super().get_quote(symbol, side, notional, path)
        upd = {}
        if self.quote_shift:
            upd["price"] = q.price * (1 + self.quote_shift)
        if self.expired_quotes:
            upd["expires_at"] = utc_now() - timedelta(seconds=1)
        q = q.model_copy(update=upd)
        self._quotes[q.quote_id] = q
        return q

    def _execute(self, order, quote, path):
        res = super()._execute(order.model_copy(update={"mode": TradingMode.PAPER}), quote, path)
        h = "0x" + hashlib.sha256(res.client_order_id.encode()).hexdigest()
        return res.model_copy(update={"mode": TradingMode.LIVE, "tx_hash": h if res.tx_hash else None})


def live_settings(**kw) -> Settings:
    base = dict(trading_mode=TradingMode.LIVE, live_trading_enabled=True, binance_live_enabled=True, has_binance_keys=True,
                risk_allow_live=True, starting_capital=10_000, sim_seed=SEED, use_fallbacks_only=True)
    return Settings(**{**base, **kw})


class ContextRisk(FallbackRiskManager):
    """A LIVE-aware risk in the shape of Bot 3's bot3/live (check(..., context=), record_execution, live_limits)."""

    def __init__(self, *a, veto=None, warnings=None, **kw):
        super().__init__(*a, **kw)
        self.contexts, self.recorded, self.veto, self.warnings = [], [], veto, warnings or []

    def check(self, signal, portfolio, control, context=None):
        v = super().check(signal, portfolio, control)
        if context is not None:
            self.contexts.append(context)
            if v.approved and self.veto and self.veto(context):
                return v.model_copy(update={"approved": False, "adjusted_notional": 0.0, "reasons": ["Blocked (live_test): vetoed"]})
            if v.approved and v.adjusted_notional > 25:
                v = v.model_copy(update={"adjusted_notional": 25.0})
        return v

    def record_execution(self, res):
        self.recorded.append(res)

    def live_limits(self, portfolio=None, context=None):
        return {"per_order_cap_usdt": 25.0, "daily_limit_usdt": 100.0, "daily_used_usdt": sum(r.notional for r in self.recorded if r.side == OrderSide.BUY),
                "daily_remaining_usdt": 80.0, "allowed_tokens": ["AAPLB"], "limit_source": {"per_order_cap_usdt": "risk"},
                "bnb_gas_reserve": 0.002, "warnings": list(self.warnings)}


def live_client(ex=None, risk=None, state_dir=None, **kw):
    s = live_settings(state_dir=state_dir, **kw)
    ex = ex or FakeLiveExchange()
    risk = risk if risk is not None else ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(
        trading_mode=TradingMode.LIVE), live_allowed=True)
    comps = build_components(s, exchange=ex, risk=risk)
    return TestClient(create_app(s, comps)), ex


def buy_strategy(size_pct=10.0, sl=2.0, activation="AUTO_EXECUTE", symbol=SYM):
    d = trading_strategy(symbol, activation=activation).model_dump(mode="json")
    d["entry"]["rsi_below"]["threshold"] = 100  # enter immediately
    d["risk"]["position_size_pct"] = size_pct
    d["exit"].update(stop_loss_pct=sl, take_profit_pct=50.0)
    d["exit"]["rsi_above"]["enabled"] = False
    return d


def first_buy_tick(c, ex, **strategy_kw):
    c.post("/api/manual/activate", json={"strategy": buy_strategy(**strategy_kw)})
    for _ in range(5):
        ds = c.post("/api/loop/tick").json()
        ex.advance()
        if ds:
            return ds
    pytest.fail("no decision")


def _buy(ds):
    return next(d for d in ds if d["signal"] and d["signal"]["action"] == "BUY")


# ---------------------------------------------------------------- explorer / execution fields
def test_explorer_url_only_for_real_live_hashes():
    h = "0x" + "ab" * 32
    assert explorer_url(h, TradingMode.LIVE) == f"https://bscscan.com/tx/{h}"
    assert explorer_url(h, TradingMode.SIM) is None and explorer_url("0xmockabc", TradingMode.LIVE) is None
    res = ExecutionResult(client_order_id="x", symbol=SYM, side=OrderSide.BUY, status=OrderStatus.FILLED,
                          path=ExecutionPath.RFQ, mode=TradingMode.LIVE, tx_hash=h).model_dump(mode="json")
    e = enrich_payload({"decision": {"execution": res}}, lambda oid: {"quote_expires_at": "T"})["decision"]["execution"]
    assert e["explorer_url"].endswith(h) and e["route"] == "RFQ" and e["real_funds"] is True and e["quote_expires_at"] == "T"
    sim = res | {"mode": "SIM"}
    assert enrich_payload(sim) == sim  # SIM/PAPER untouched


def test_sim_payloads_stay_contract_shaped():
    from backend.contracts import Decision
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    buy, _ = tick_until(c, lambda d: d["action"] == "BUY")
    assert buy["execution"]["tx_hash"] and not {"explorer_url", "route", "real_funds"} & set(buy["execution"])
    for d in c.get("/api/decisions").json():
        Decision.model_validate(d)


# ---------------------------------------------------------------- autonomous LIVE: Bot 3 two-pass sequence
def test_live_buy_is_autonomous_two_pass_and_carries_tx_fields():
    c, ex = live_client()
    risk = c.app.state.st.loop.c.risk
    c.post("/api/control/confirmation-mode", json={"enabled": False})
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "BUY" and d["execution"]["status"] == "FILLED", d["reason"]
    assert not c.get("/api/confirmations").json()  # no approval step
    # pass 1 (no quote) and pass 2 (quote + fresh now), with balances / data timestamp / bar
    assert len(risk.contexts) == 2 and risk.contexts[0].quote is None and risk.contexts[1].quote is not None
    c1, c2 = risk.contexts
    assert c2.now >= c1.now and c1.balances == {"BNB": 0.05, "USDT": 200.0} and c1.reference_price > 0
    assert c1.data_timestamp is not None and c1.bar is not None and c1.bar_interval == timedelta(hours=1)
    assert c2.quote.notional == pytest.approx(min(25.0, c2.quote.notional))
    assert ex.executions[-1].avg_price == c2.quote.price and risk.recorded and risk.recorded[-1].status == OrderStatus.FILLED
    e = d["execution"]
    assert e["explorer_url"] == "https://bscscan.com/tx/" + e["tx_hash"] and e["real_funds"] is True
    assert e["route"] == e["path"] and e["quote_expires_at"]
    assert c.get(f"/api/decisions/{d['id']}").json()["execution"]["explorer_url"] == e["explorer_url"]
    assert c.get("/api/status").json()["last_decision"]["execution"]["route"] == e["path"]


def test_live_confirmation_only_when_mode_on():
    c, ex = live_client()
    ds = first_buy_tick(c, ex, activation="CONFIRMATION")
    d = _buy(ds)
    assert d["execution"]["status"] == "PENDING_CONFIRMATION" and not ex.executions
    assert "quote" not in json.dumps(d["execution"]).lower().replace("quote_id", "")  # no pending-confirmation quote fields
    a = c.post(f"/api/confirmations/{d['id']}/approve").json()
    assert a["execution"]["status"] == "FILLED" and a["execution"]["real_funds"]


def test_live_ws_execution_event_carries_tx_fields():
    c, ex = live_client()
    c.post("/api/manual/activate", json={"strategy": buy_strategy()})
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        c.post("/api/loop/tick")
        for _ in range(60):
            ev = ws.receive_json()
            if ev["type"] == "EXECUTION":
                p = ev["payload"]
                assert p["explorer_url"] == "https://bscscan.com/tx/" + p["tx_hash"] and p["route"] in ("SWAP", "RFQ")
                assert p["real_funds"] is True and p["quote_expires_at"]
                break
        else:
            pytest.fail("no EXECUTION event")


@pytest.mark.parametrize("setup,needle", [
    (lambda ex: setattr(ex, "get_balances", None), "live_wallet_balances"),
    (lambda ex: setattr(ex, "get_quote", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("market closed (40367)"))), "market closed"),
])
def test_live_blocks_are_normal_decisions(setup, needle):
    c, ex = live_client()
    setup(ex)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "STAY_OUT" and needle in d["reason"] and not ex.executions


def test_live_risk_veto_on_second_pass():
    risk = ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(trading_mode=TradingMode.LIVE),
                       live_allowed=True, veto=lambda ctx: ctx.quote is not None)
    c, ex = live_client(risk=risk)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "STAY_OUT" and "live_test" in d["reason"] and not ex.executions


def test_live_xstock_excluded_by_default():
    c, ex = live_client()
    d = first_buy_tick(c, ex, symbol="METAx")
    assert all(x["action"] != "BUY" for x in d) and any("xStock" in x["reason"] for x in d)


@pytest.mark.parametrize("setup,check", [
    (lambda ex: ex.balances.update(USDT=5.0), "live_usdt_balance"),
    (lambda ex: ex.balances.update(BNB=0.0), "live_gas_reserve"),
    (lambda ex: setattr(ex, "quote_shift", 0.10), "live_slippage"),
])
def test_legacy_risk_uses_app_caps(setup, check):
    """A risk without `context` (PR #4 / stand-in): the app's RISK_LIVE_* caps guard the BUY."""
    from backend.contracts import ControlState
    c, ex = live_client(risk=FallbackRiskManager(control=ControlState(trading_mode=TradingMode.LIVE), live_allowed=True))
    setup(ex)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "STAY_OUT" and check in d["reason"] and not ex.executions


def test_legacy_risk_clamps_to_per_order_cap():
    from backend.contracts import ControlState
    c, ex = live_client(risk=FallbackRiskManager(control=ControlState(trading_mode=TradingMode.LIVE), live_allowed=True),
                        live_max_order_usdt=15.0, ex=FakeLiveExchange(balances={"BNB": 0.05, "USDT": 1000.0}))
    d = _buy(first_buy_tick(c, ex, size_pct=40.0))
    assert d["execution"]["status"] == "FILLED" and ex.executions[-1].notional + ex.executions[-1].fee <= 15.0 + 1e-6


def test_app_live_checks_unit():
    s = live_settings()
    sig = Signal(symbol=SYM, action=Action.BUY, source=AgentMode.MANUAL, strategy="x", reason="x", proposed_notional=10)
    pf = Portfolio(starting_capital=100, cash=100, equity=100)
    now = utc_now()
    q = Quote(symbol=SYM, side=OrderSide.BUY, notional=10, quantity=0.1, price=100, created_at=now, expires_at=now + timedelta(seconds=30))
    assert all(c.passed for c in app_live_checks(s, sig, pf, 10, {"USDT": 50, "BNB": 0.01}, q, 100.5, now, 0.0, now))
    bad = {c.name for c in app_live_checks(s, sig, pf, 30, {"USDT": 50, "BNB": 0.01}, q, 100, now - timedelta(seconds=9999), 80.0, now) if not c.passed}
    assert bad == {"live_per_order_cap", "live_daily_notional_cap", "live_stale_data"}


def test_live_exits_run_automatically():
    c, ex = live_client()
    d = _buy(first_buy_tick(c, ex, sl=2.0))
    assert d["execution"]["status"] == "FILLED"
    entry = ex.executions[-1].avg_price
    last = ex.series[ex.cursor - 1]
    ex.push(Candle(timestamp=last.timestamp + timedelta(hours=1), open=entry * 0.99, high=entry * 0.995, low=entry * 0.9,
                   close=entry * 0.91, volume=last.volume))
    c.post("/api/control/confirmation-mode", json={"enabled": True})
    sells = [x for x in c.post("/api/loop/tick").json() if x["action"] == "SELL"]
    assert sells and sells[0]["execution"]["status"] == "FILLED" and sells[0]["reason"].startswith("STOP_LOSS")
    assert sells[0]["execution"]["explorer_url"].startswith("https://bscscan.com/tx/0x")


@pytest.mark.skipif(not _has_bot3_live(), reason="bot3/live LiveContext not on this branch")
def test_with_bot3_live_risk(monkeypatch):
    from backend.risk import DefaultRiskManager
    for k in ("RISK_ALLOW_LIVE", "LIVE_TRADING_ENABLED", "BINANCE_LIVE_ENABLED"):
        monkeypatch.setenv(k, "true")
    from backend.contracts import ControlState
    risk = DefaultRiskManager(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False))
    c, ex = live_client(risk=risk, ex=FakeLiveExchange(balances={"BNB": 0.05, "USDT": 1000.0}))
    ds = first_buy_tick(c, ex)
    d = _buy(ds)
    assert d["action"] in ("BUY", "STAY_OUT")
    if d["action"] == "BUY":
        assert d["execution"]["status"] == "FILLED" and d["execution"]["notional"] <= 25.0 + 1e-6 and d["execution"]["real_funds"]
    else:  # whatever Bot 3 blocks on must be a normal reason
        assert d["reason"].startswith("BUY not executed")
    w = c.get("/api/wallet").json()
    assert w["limits"]["per_order_cap_usdt"] and "limit_source" in w["limits"] and isinstance(w["limits"]["warnings"], list)
    assert c.get("/api/live/status").json()["risk_allow_live"] is True


# ---------------------------------------------------------------- wallet / live status / system
def test_wallet_sim_shape():
    c = make_client()
    w = c.get("/api/wallet").json()
    assert w["provider"] == "sim" and w["chain"] == "BSC" and w["chain_id"] == 56 and w["connected"] and w["address"]
    assert {"symbol", "amount", "usd_value"} == set(w["balances"][0])
    assets = {b["symbol"]: b for b in w["balances"]}
    assert assets["USDT"]["amount"] == c.get("/api/portfolio").json()["cash"] and assets["BNB"]["amount"] > 0
    assert {"daily_limit_usdt", "daily_used_usdt", "per_order_cap_usdt", "allowed_tokens", "warnings"} <= set(w["limits"])
    assert w["limits"]["warnings"] == [] and w["bnb_gas_reserve"] > 0 and w["updated_at"]


def test_wallet_live_merges_risk_limits_and_alerts_new_warnings():
    risk = ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(trading_mode=TradingMode.LIVE),
                       live_allowed=True, warnings=[{"code": "quota_exit", "message": "quota cannot cover selling all positions"}])
    c, ex = live_client(risk=risk)
    w = c.get("/api/wallet").json()
    assert w["provider"] == "baw" and w["connected"] and w["address"] == ex.wallet_address
    assert {b["symbol"]: b["amount"] for b in w["balances"]} == {"BNB": 0.05, "USDT": 200.0}
    lim = w["limits"]
    assert lim["per_order_cap_usdt"] == 25.0 and lim["daily_limit_usdt"] == 100.0 and lim["daily_remaining_usdt"] == 80.0
    assert lim["allowed_tokens"] == ["AAPLB"] and lim["limit_source"] == {"per_order_cap_usdt": "risk"}
    assert lim["warnings"] == [{"code": "quota_exit", "message": "quota cannot cover selling all positions"}]
    c.get("/api/wallet")
    al = [a for a in c.get("/api/alerts").json() if a["code"] == "LIVE_LIMIT_QUOTA_EXIT"]
    assert len(al) == 1  # once per new code

    c, ex = live_client(ex=FakeLiveExchange(wallet=False))
    w = c.get("/api/wallet").json()
    assert not w["connected"] and w["balances"] == []


def test_live_status_and_system_block():
    c, _ = live_client()
    ls = c.get("/api/live/status").json()
    assert ls == {"live_trading_enabled": True, "binance_live_enabled": True, "risk_allow_live": True, "wallet_connected": True,
                  "can_trade": True, "blocked_reasons": []}
    assert c.get("/api/system").json()["live"] == ls
    c.post("/api/control/emergency-stop", json={"reason": "test"})
    ls = c.get("/api/live/status").json()
    assert not ls["can_trade"] and any("Emergency stop" in r for r in ls["blocked_reasons"])

    c, _ = live_client(ex=FakeLiveExchange(wallet=False))
    ls = c.get("/api/live/status").json()
    assert not ls["wallet_connected"] and not ls["can_trade"]

    ls = make_client().get("/api/system").json()["live"]
    assert not ls["can_trade"] and "TRADING_MODE is SIM, not LIVE" in ls["blocked_reasons"]


# ---------------------------------------------------------------- wallet settings (baw) - never run for real in tests
def test_wallet_settings_provider_never_runs_outside_live_and_caches():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return json.dumps({"dailyLimit": "100", "quotaUsed": "10", "quotaLeft": "90"})

    sim = WalletSettingsProvider(object(), TradingMode.SIM, runner=runner)
    assert sim.get() is None and not calls
    live = WalletSettingsProvider(object(), TradingMode.LIVE, runner=runner, ttl_s=60)
    live.get(); live.get()
    assert len(calls) == 1 and calls[0][:3] == ("baw", "wallet", "settings")

    errs = []
    bad = WalletSettingsProvider(object(), TradingMode.LIVE, runner=lambda cmd: (_ for _ in ()).throw(FileNotFoundError("baw")),
                                 on_error=errs.append, ttl_s=0)
    assert bad.get() is None and bad.get() is None and len(errs) == 1  # one alert per failure streak

    class ExWithSettings:
        def get_wallet_settings(self):
            return {"dailyLimit": 50}
    p = WalletSettingsProvider(ExWithSettings(), TradingMode.LIVE, runner=runner)
    p.get()
    assert len(calls) == 1 and p._raw == {"dailyLimit": 50}  # exchange first, no subprocess


def test_live_loop_never_spawns_baw_in_tests(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess in test")))
    c, ex = live_client()
    c.app.state.st.loop.wallet_settings.runner = lambda cmd: json.dumps({"dailyLimit": 100})
    _buy(first_buy_tick(c, ex))
    c.get("/api/wallet")


# ---------------------------------------------------------------- 24/7: persistence, heartbeat, watchdog, autostart
def test_state_survives_restart(tmp_path):
    c, ex = live_client(state_dir=str(tmp_path))
    d = _buy(first_buy_tick(c, ex, sl=30.0))
    assert d["execution"]["status"] == "FILLED"
    c.post("/api/control/emergency-stop", json={"reason": "manual test"})
    assert list(tmp_path.glob("agent_state.live.json"))
    c2, _ = live_client(state_dir=str(tmp_path), ex=ex)
    st = c2.get("/api/status").json()
    assert st["control"]["emergency_stopped"] and st["active_symbol"] == SYM and len(st["portfolio"]["positions"]) == 1
    assert c2.get("/api/wallet").json()["limits"]["daily_used_usdt"] > 0  # app ledger persisted
    assert any(a["code"] == "STATE_RESTORED" for a in c2.get("/api/alerts").json())
    # SIM state never leaks into LIVE
    assert not list(tmp_path.glob("agent_state.sim.json"))


def test_heartbeat_and_stall_alert():
    from backend.app.main import check_stall
    c, ex = live_client()
    hb = c.get("/api/loop").json()["heartbeat"]
    assert hb["uptime_s"] >= 0 and hb["last_tick_at"] is None and hb["restarts"] == 0
    c.post("/api/manual/activate", json={"strategy": buy_strategy()})
    c.post("/api/loop/tick")
    assert c.get("/api/loop").json()["heartbeat"]["last_tick_at"]
    st = c.app.state.st
    st.want_running, st.interval = True, 1.0
    st.loop.last_tick_at = utc_now() - timedelta(seconds=10)
    assert check_stall(st) and check_stall(st)
    assert len([a for a in c.get("/api/alerts").json() if a["code"] == "LOOP_STALLED"]) == 1


def test_watchdog_restarts_crashed_loop():
    import asyncio
    from backend.app import main as m
    c, ex = live_client()
    st = c.app.state.st
    st.interval = 0.01
    crashes = {"n": 0}
    real_run = m._run

    async def flaky(s):
        if crashes["n"] < 2:
            crashes["n"] += 1
            raise RuntimeError("boom")
        s.want_running = False

    async def go():
        m._run = flaky
        orig_sleep = asyncio.sleep
        try:
            st.want_running = True
            m.asyncio.sleep = lambda t: orig_sleep(0)
            await m._supervise(st)
        finally:
            m._run = real_run
            m.asyncio.sleep = orig_sleep
    asyncio.run(go())
    assert st.loop.restarts == 2 and len([a for a in c.get("/api/alerts").json() if a["code"] == "LOOP_RESTARTED"]) == 2


def test_autostart_resumes_saved_agent_and_loop(tmp_path):
    c, ex = live_client(state_dir=str(tmp_path))
    c.post("/api/manual/activate", json={"strategy": buy_strategy()})
    c2, _ = live_client(state_dir=str(tmp_path), ex=ex, loop_autostart=True, loop_interval_seconds=0.05)
    with c2:
        lp = c2.get("/api/loop").json()
        assert lp["running"] and lp["active_symbol"] == SYM and lp["heartbeat"]["autostart"]
        c2.post("/api/loop/stop")


def test_autostart_default_agent_is_auto(tmp_path):
    c, _ = live_client(state_dir=str(tmp_path), loop_autostart=True, default_agent_symbol=SYM, loop_interval_seconds=0.05)
    with c:
        lp = c.get("/api/loop").json()
        assert lp["running"] and lp["agent_mode"] == "AUTO" and lp["active_symbol"] == SYM
        c.post("/api/loop/stop")

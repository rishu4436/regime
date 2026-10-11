"""LIVE flow with mocks (no network, no real money): wallet view, explorer links, BUYs always need the user,
pre-trade checks (Bot 3 check_live duck-typed + app hard caps), exits stay automatic, allow_live in /api/system."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Optional

import pytest
from fastapi.testclient import TestClient

import json

from backend.app.fallbacks import FallbackRiskManager
from backend.app.live import WalletSettingsProvider, app_live_checks, enrich_payload, explorer_url
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import (
    Action, AgentMode, Candle, ExitReason, ExecutionResult, ExecutionPath, OrderSide, OrderStatus, Portfolio, Quote, Signal,
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
                  "can_trade": True, "blocked_reasons": [], "cooldowns": [], "chain_pending": []}
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
    assert len(calls) == 1 and calls[0][0].replace("\\", "/").rsplit("/", 1)[-1] in ("baw", "baw.cmd") and calls[0][1:3] == ("wallet", "settings")

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


# ---------------------------------------------------------------- exchange states: cooldown, session, chain, market context
class QuoteX(Quote):
    """Bot 4 PR #9 quotes carry price impact (percent units)."""
    price_impact_pct: Optional[float] = None


def test_off_hours_error_sets_20_min_cooldown():
    c, ex = live_client()
    ex.get_quote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("QuoteUnavailable: bStock market closed (40369)"))
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "STAY_OUT" and "40369" in d["reason"] and "cooling down until" in d["reason"]
    cds = c.get("/api/live/status").json()["cooldowns"]
    assert cds and cds[0]["symbol"] == SYM and cds[0]["code"] == "40369"
    ds = c.post("/api/loop/tick").json()
    assert any("cooling down" in x["reason"] and x["action"] == "STAY_OUT" for x in ds)


def test_wallet_session_expired_pauses_and_alerts():
    c, ex = live_client()
    ex.get_quote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("WALLET_SESSION_EXPIRED: baw session expired"))
    _buy(first_buy_tick(c, ex))
    ctrl = c.get("/api/control").json()
    assert ctrl["paused"] and "session expired" in ctrl["reason"].lower()
    assert any(a["code"] == "WALLET_SESSION_EXPIRED" for a in c.get("/api/alerts").json())
    assert any("Paused" in r for r in c.get("/api/live/status").json()["blocked_reasons"])


def test_waiting_on_chain_then_confirmed():
    class ChainEx(FakeLiveExchange):
        def _execute(self, order, quote, path):
            res = super()._execute(order, quote, path)
            self.final = res
            return res.model_copy(update={"status": OrderStatus.PENDING_CONFIRMATION, "filled_quantity": 0.0})

        def refresh_execution(self, res):
            return self.final
    c, ex = live_client(ex=ChainEx())
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "BUY" and "waiting on chain" in d["reason"] and d["execution"]["status"] == "PENDING_CONFIRMATION"
    pos = c.get("/api/portfolio").json()["positions"]
    assert len(pos) == 1 and pos[0]["pending"] is True and pos[0]["client_order_id"] == d["execution"]["client_order_id"]
    assert c.get("/api/live/status").json()["chain_pending"]
    oid = d["execution"]["order_id"]
    c.post("/api/control/pause", json={"reason": "no new entries"})
    c.post("/api/loop/tick")
    pos = c.get("/api/portfolio").json()["positions"]
    assert len(pos) == 1 and pos[0]["pending"] is False and pos[0]["held_exit"] is None
    assert oid not in [p["order_id"] for p in c.get("/api/live/status").json()["chain_pending"]]
    d2 = c.get(f"/api/decisions/{d['id']}").json()
    assert d2["execution"]["status"] == "FILLED" and "confirmed on chain" in d2["reason"]


def test_engine_market_context_and_price_impact_passthrough():
    import dataclasses
    from backend.app.fallbacks import FallbackStrategyEngine

    @dataclasses.dataclass
    class Cfg:
        max_order_usdt: float = 99.0

    class SpyEngine(FallbackStrategyEngine):
        live_config = Cfg()
        seen: list = []

        def set_market_context(self, symbol, ctx):
            self.seen.append((symbol, ctx))

    class ImpactEx(FakeLiveExchange):
        impact = 0.35

        def get_quote(self, symbol, side, notional, path=ExecutionPath.SWAP):
            q = super().get_quote(symbol, side, notional, path)
            q = QuoteX(**q.model_dump(), price_impact_pct=self.impact)
            self._quotes[q.quote_id] = q
            return q

        def get_token_status(self, symbol):
            return {"trading_status": "trading", "min_order_usdt": None, "reference_price_only": False}

    eng = SpyEngine()
    s = live_settings()
    risk = ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(trading_mode=TradingMode.LIVE), live_allowed=True)
    ex = ImpactEx()
    c = TestClient(create_app(s, build_components(s, exchange=ex, risk=risk, engine=eng)))
    d = _buy(first_buy_tick(c, ex))
    assert d["execution"]["status"] == "FILLED"
    sym, ctx = eng.seen[0]
    assert sym == SYM and ctx == {"status": "trading", "reference_price_only": False, "min_order_usdt": None,
                                   "priceImpactPercent": 0.35, "equity": 200.0}
    assert eng.live_config.max_order_usdt == 25.0  # from risk.live_limits()['per_order_cap_usdt']
    assert risk.contexts[1].price_impact_pct == 0.35 and risk.contexts[1].token_status is not None
    eng2, ex2 = SpyEngine(), ImpactEx()
    eng2.seen = []
    ex2.impact = None  # null -> None, never 0
    risk2 = ContextRisk(control=__import__("backend.contracts", fromlist=["ControlState"]).ControlState(trading_mode=TradingMode.LIVE), live_allowed=True)
    c2 = TestClient(create_app(s, build_components(s, exchange=ex2, risk=risk2, engine=eng2)))
    first_buy_tick(c2, ex2)
    assert "priceImpactPercent" not in eng2.seen[-1][1] and risk2.contexts and risk2.contexts[-1].price_impact_pct is None


def test_agent_identity_in_system(monkeypatch):
    import sys
    import types
    c, _ = live_client()
    monkeypatch.setitem(sys.modules, "backend.agent_studio", types.SimpleNamespace(get_agent_identity=lambda: None))
    assert c.get("/api/system").json()["agent_identity"] is None
    ident = {"erc8004_id": "42", "registry": "0xabc", "studio_version": "1.0", "network": "bsc",
             "explorer_url": "https://bscscan.com/address/0xabc"}
    monkeypatch.setitem(sys.modules, "backend.agent_studio", types.SimpleNamespace(get_agent_identity=lambda: ident))
    import backend
    monkeypatch.setattr(backend, "agent_studio", sys.modules["backend.agent_studio"], raising=False)
    assert c.get("/api/system").json()["agent_identity"] == ident  # every key passed through
    monkeypatch.delitem(sys.modules, "backend.agent_studio")
    monkeypatch.setattr(backend, "agent_studio", None, raising=False)
    assert c.get("/api/system").json()["agent_identity"] is None


# ---------------------------------------------------------------- engine <-> risk agreement on the price-impact block
def _parity_available() -> bool:
    try:
        from backend.engine.engine import DefaultStrategyEngine
        from backend.risk import LiveContext
        return "price_impact_pct" in LiveContext.model_fields and hasattr(DefaultStrategyEngine, "set_market_context")
    except Exception:
        return False


@pytest.mark.skipif(not _parity_available(), reason="needs bot2/live engine (set_market_context) and bot3/live LiveContext.price_impact_pct")
@pytest.mark.parametrize("impact", [0.5, 0.99, 1.0, 1.01, 1.5, 2.5])
def test_engine_and_risk_agree_on_price_impact_threshold(impact):
    """Both block a LIVE BUY when price impact > 1% and allow exactly 1%."""
    from backend.contracts import ControlState
    from backend.engine.engine import DefaultStrategyEngine
    from backend.risk import DefaultRiskManager, LiveContext, RiskConfig
    now = utc_now()
    ex = FakeLiveExchange()
    shift = (now - timedelta(minutes=30)) - ex.series[ex.cursor - 1].timestamp
    candles = [b.model_copy(update={"timestamp": b.timestamp + shift}) for b in ex.series[:ex.cursor]]
    strat = trading_strategy(SYM, activation="AUTO_EXECUTE").model_copy(deep=True)
    d = strat.model_dump()
    d["entry"]["rsi_below"]["threshold"] = 100
    from backend.contracts import ManualStrategy
    strat = ManualStrategy.model_validate(d)
    pf = Portfolio(starting_capital=1000, cash=1000, equity=1000, mode=TradingMode.LIVE)

    eng = DefaultStrategyEngine(live_safe=True, clock=lambda: now)
    eng.set_market_context(SYM, {"status": "trading", "reference_price_only": False, "priceImpactPercent": impact, "equity": 1000})
    sig = eng.evaluate_manual(strat, candles, pf)
    engine_blocks = sig.action != Action.BUY and "impact" in (sig.reason + " ".join(sig.conditions_failed or [])).lower()
    if sig.action != Action.BUY and not engine_blocks:
        pytest.skip(f"engine stays out for another reason: {sig.reason}")

    risk = DefaultRiskManager(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False),
                              config=RiskConfig(allow_live=True))
    price = candles[-1].close
    q = Quote(symbol=SYM, side=OrderSide.BUY, notional=20, quantity=20 / price, price=price, created_at=now,
              expires_at=now + timedelta(seconds=30))
    ctx = LiveContext(now=now, data_timestamp=now, reference_price=price, quote=q, balances={"USDT": 1000.0, "BNB": 0.05},
                      price_impact_pct=impact)
    buy = Signal(symbol=SYM, action=Action.BUY, source=AgentMode.MANUAL, strategy="x", reason="x", proposed_notional=20, price=price)
    v = risk.check(buy, pf, risk.get_control_state(), context=ctx)
    impact_checks = [c for c in v.checks if "impact" in c.name or "impact" in c.detail.lower()]
    risk_blocks = any(not c.passed for c in impact_checks)

    assert engine_blocks == (impact > 1.0), (impact, sig.reason)
    assert risk_blocks == (impact > 1.0), (impact, [c.model_dump() for c in impact_checks])


def test_wallet_allowed_tokens_is_risk_only_and_tradable_tokens_from_exchange():
    c, _ = live_client()
    lim = c.get("/api/wallet").json()["limits"]
    toks = [t["symbol"] for t in c.get("/api/tokens").json()]
    assert lim["tradable_tokens"] == [t for t in toks if not t.endswith("x")]
    assert lim["allowed_tokens"] != lim["tradable_tokens"]  # never filled from the exchange list


def test_auto_on_nvdab_in_sim():
    """Default live demo agent is Auto on NVDAB (Bot 4 0770978 lists it); it must also activate and tick in SIM."""
    c = TestClient(create_app(Settings(sim_seed=SEED)))
    if "NVDAB" not in [t["symbol"] for t in c.get("/api/tokens").json()]:
        pytest.skip("exchange token list has no NVDAB (needs bot4 0770978)")
    r = c.post("/api/auto/activate", json={"symbol": "NVDAB", "capital": 1000, "activation": "AUTO_EXECUTE"})
    assert r.status_code == 200, r.text
    ds = []
    for _ in range(80):
        ds += c.post("/api/loop/tick").json()
    assert ds and all(d["symbol"] == "NVDAB" for d in ds)
    assert c.get("/api/status").json()["active_symbol"] == "NVDAB"
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]


# ---------------------------------------------------------------- in-flight LIVE buys (synthetic pending positions)
class FakeExecutor:
    """Bot 4 BawExecutor surface used by the app: intents.open_intents(), reconcile(), extras, max_slippage_pct."""

    def __init__(self):
        self.journal, self.extras, self.max_slippage_pct, self.submitted = {}, {}, 3.0, 0
        self.intents = self

    def open_intents(self):
        return [dict(r) for r in self.journal.values() if r["state"] in ("SUBMITTING", "SUBMITTED", "PENDING", "UNKNOWN")]

    def reconcile(self, client_order_id=None):
        out = []
        for r in self.journal.values():
            if r["state"] in ("FINISHED", "FAILED") and not r.get("_seen"):
                r["_seen"] = True
                out.append({k: v for k, v in r.items() if k != "_seen"})
        return out

    def settle(self, cid, state, to_qty=None):
        self.journal[cid].update(state=state, to_qty=to_qty, order_id="w-1",
                                 tx_hash=("0x" + "ab" * 32) if state == "FINISHED" else None)


class InflightEx(FakeLiveExchange):
    """LIVE BUYs come back PENDING_CONFIRMATION (outcome unknown) and are journaled; SELLs fill."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.executor = FakeExecutor()
        self.refuse_in_flight = False

    def _execute(self, order, quote, path):
        if order.side != OrderSide.BUY:
            return super()._execute(order, quote, path)
        cid = order.client_order_id
        if self.refuse_in_flight:
            return ExecutionResult(client_order_id=cid, symbol=order.symbol, side=order.side, status=OrderStatus.REJECTED,
                                   path=path, mode=TradingMode.LIVE, error="[order_in_flight] another LIVE swap is in flight in this process")
        self.executor.submitted += 1
        self.executor.journal[cid] = {"client_order_id": cid, "state": "SUBMITTED", "symbol": order.symbol, "side": "BUY",
                                      "from_qty": str(quote.notional)}
        self.executor.extras[cid] = {"real_funds": True, "route": "SWAP", "explorer_url": "https://bscscan.com/tx/0xfromextras",
                                     "gas": {"bnb": 0.00012}, "reason": None}
        return ExecutionResult(client_order_id=cid, symbol=order.symbol, side=order.side, status=OrderStatus.PENDING_CONFIRMATION,
                               path=path, mode=TradingMode.LIVE, error="[outcome_unknown] waiting on chain")


class SLRisk(ContextRisk):
    """Stop-loss hit on every position each bar (incl. synthetic pending ones), or flatten on emergency stop."""

    def __init__(self, *a, sl=False, **kw):
        super().__init__(*a, **kw)
        self.sl, self.portfolios = sl, []

    def check(self, signal, portfolio, control, context=None):
        self.portfolios.append(portfolio)
        return super().check(signal, portfolio, control, context)

    def check_exits(self, portfolio, prices, bars=None):
        out = []
        for p in portfolio.positions:
            why = ExitReason.STOP_LOSS if self.sl else (ExitReason.EMERGENCY_STOP if self.get_control_state().emergency_stopped else None)
            if why:
                out.append(Signal(symbol=p.symbol, action=Action.SELL, source=AgentMode.MANUAL, strategy=p.strategy,
                                  reason=f"{why.value} hit", exit_reason=why, position_id=p.id, price=prices.get(p.symbol)))
        return out


def inflight(sl=False):
    from backend.contracts import ControlState
    risk = SLRisk(control=ControlState(trading_mode=TradingMode.LIVE), live_allowed=True)
    c, ex = live_client(ex=InflightEx(), risk=risk)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "BUY" and d["execution"]["status"] == "PENDING_CONFIRMATION"
    risk.sl = sl
    return c, ex, risk, d["execution"]["client_order_id"]


def _tick(c, ex):
    ex.advance()
    return c.post("/api/loop/tick").json()


def test_pending_buy_recorded_and_no_second_buy():
    c, ex, risk, cid = inflight()
    assert any(r.status == OrderStatus.PENDING_CONFIRMATION for r in risk.recorded)  # record_execution at submission
    for _ in range(3):
        assert not [d for d in _tick(c, ex) if d["action"] == "BUY"]
    assert ex.executor.submitted == 1
    pos = c.get("/api/portfolio").json()["positions"]
    assert [(p["pending"], p["client_order_id"], p["held_exit"]) for p in pos] == [(True, cid, None)]
    st = c.get("/api/status").json()["portfolio"]["positions"]
    assert st[0]["pending"] is True and st[0]["client_order_id"] == cid
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]


def test_pending_buy_counts_toward_exposure_and_positions():
    c, ex, risk, cid = inflight()
    loop = c.app.state.st.loop if hasattr(c.app.state, "st") else None
    risk.portfolios.clear()
    c.post("/api/manual/activate", json={"strategy": buy_strategy()})
    _tick(c, ex)
    seen = [pf for pf in risk.portfolios if pf.positions]
    pf = (seen or [None])[-1] or (loop._pf() if loop else None)
    assert pf is not None and [p.id for p in pf.positions] == [f"pending-{cid}"]
    assert pf.cash < 10_000 - 1  # the quote amount is committed
    assert c.get("/api/wallet").json()["limits"]["daily_used_usdt"] > 0


def test_pending_to_filled_replaces_synthetic_position_with_extras():
    c, ex, risk, cid = inflight()
    ex.executor.settle(cid, "FINISHED", to_qty="0.1")
    ds = _tick(c, ex)
    conf = next(d for d in ds if "confirmed on chain" in d["reason"])
    assert conf["execution"]["status"] == "FILLED" and conf["execution"]["explorer_url"] == "https://bscscan.com/tx/0xfromextras"
    assert conf["execution"]["gas"] == {"bnb": 0.00012} and conf["execution"]["real_funds"] is True
    pos = c.get("/api/portfolio").json()["positions"]
    assert len(pos) == 1 and pos[0]["pending"] is False and pos[0]["quantity"] == 0.1
    assert not c.get("/api/live/status").json()["chain_pending"]
    assert ex.executor.max_slippage_pct == 1.0  # capped to risk / app slippage cap


def test_pending_to_failed_drops_synthetic_position():
    c, ex, risk, cid = inflight()
    ex.executor.settle(cid, "FAILED")
    ds = _tick(c, ex)
    assert any("BUY failed on chain" in d["reason"] for d in ds)
    # the failed order is gone (the always-buy test strategy may place a fresh one in the same tick)
    assert cid not in [p["client_order_id"] for p in c.get("/api/portfolio").json()["positions"]]


def test_pending_with_stop_loss_is_held_not_sold():
    c, ex, risk, cid = inflight(sl=True)
    ds = _tick(c, ex)
    held = [d for d in ds if d["reason"].startswith("exit held: buy still settling on-chain")]
    assert held and "pending_exit" in held[0]["signal"]["conditions_failed"]
    assert not [d for d in ds if d["action"] == "SELL"] and len(ex.executions) == 0 or all(e.side == OrderSide.BUY for e in ex.executions)
    pos = c.get("/api/portfolio").json()["positions"][0]
    assert pos["pending"] and pos["held_exit"] == {"code": "pending_exit", "reason": "STOP_LOSS", "source": "risk.check_exits"}


def test_pending_emergency_stop_sells_in_the_tick_the_fill_confirms():
    c, ex, risk, cid = inflight()
    c.post("/api/control/emergency-stop", json={"reason": "test", "flatten": True})
    pos = c.get("/api/portfolio").json()["positions"][0]
    assert pos["held_exit"]["code"] == "pending_exit" and pos["held_exit"]["source"] == "emergency_stop"
    ex.executor.settle(cid, "FINISHED", to_qty="0.1")
    ds = _tick(c, ex)
    assert any("confirmed on chain" in d["reason"] for d in ds)
    assert any(d["action"] == "SELL" for d in ds)
    assert not c.get("/api/portfolio").json()["positions"]


def test_pending_failed_drops_held_exit():
    c, ex, risk, cid = inflight(sl=True)
    _tick(c, ex)
    ex.executor.settle(cid, "FAILED")
    ds = _tick(c, ex)
    assert any("held exit dropped" in d["reason"] for d in ds) and not [d for d in ds if d["action"] == "SELL"]
    assert cid not in [p["client_order_id"] for p in c.get("/api/portfolio").json()["positions"]]
    assert all(e.side == OrderSide.BUY for e in ex.executions)


def test_order_in_flight_is_a_normal_stay_out():
    ex = InflightEx()
    ex.refuse_in_flight = True
    c, ex = live_client(ex=ex)
    d = _buy(first_buy_tick(c, ex))
    assert d["action"] == "STAY_OUT" and "order_in_flight" in d["reason"] and "order_in_flight" in d["signal"]["conditions_failed"]
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]


def test_open_intent_from_journal_becomes_pending_after_restart():
    ex = InflightEx()
    ex.executor.journal["restored-1"] = {"client_order_id": "restored-1", "state": "SUBMITTED", "symbol": SYM, "side": "BUY",
                                         "from_qty": "20"}
    c, ex = live_client(ex=ex)
    c.post("/api/manual/activate", json={"strategy": buy_strategy()})
    ds = _tick(c, ex)
    assert not [d for d in ds if d["action"] == "BUY"]
    pos = c.get("/api/portfolio").json()["positions"]
    assert [(p["client_order_id"], p["pending"]) for p in pos] == [("restored-1", True)]
    assert ex.executor.submitted == 0


def test_slippage_cap_passed_per_order_when_supported():
    class SlipEx(InflightEx):
        seen = []

        def execute_swap(self, order, quote, max_slippage_pct=None):
            self.seen.append(max_slippage_pct)
            return super().execute_swap(order, quote)
    ex = SlipEx()
    c, ex = live_client(ex=ex)
    _buy(first_buy_tick(c, ex))
    assert ex.seen == [1.0]  # risk / RISK_LIVE_MAX_SLIPPAGE_PCT cap, per order
    assert ex.executor.max_slippage_pct == 3.0  # no executor-wide workaround when the argument exists


def test_record_execution_with_quote_notional_at_submission_and_after_reconcile():
    from backend.contracts import ControlState

    class QNRisk(SLRisk):
        def record_execution(self, res, quote_notional=None):
            self.recorded.append(res)
            self.qn = getattr(self, "qn", []) + [(res.client_order_id, res.status.value, quote_notional)]
    risk = QNRisk(control=ControlState(trading_mode=TradingMode.LIVE), live_allowed=True)
    c, ex = live_client(ex=InflightEx(), risk=risk)
    d = _buy(first_buy_tick(c, ex))
    cid = d["execution"]["client_order_id"]
    sub = [x for x in risk.qn if x[0] == cid]
    assert len(sub) == 1 and sub[0][1] == "PENDING_CONFIRMATION" and sub[0][2] and sub[0][2] > 0
    ex.executor.settle(cid, "FINISHED", to_qty="0.1")
    _tick(c, ex)
    sub = [x for x in risk.qn if x[0] == cid]
    assert [s[1] for s in sub] == ["PENDING_CONFIRMATION", "FILLED"] and sub[1][2] == sub[0][2]


# ---------------------------------------------------------------- Bot 1: token_status, autonomy by default, monitor fields
def test_tokens_carry_token_status_cached():
    class StatusEx(MockExchangeClient):
        calls = 0

        def get_token_status(self, symbol):
            StatusEx.calls += 1
            return {"trading_status": "trading", "min_order_usdt": 20.0 if symbol.endswith("on") else None}
    s = Settings(sim_seed=SEED, use_fallbacks_only=True)
    c = TestClient(create_app(s, build_components(s, exchange=StatusEx(seed=SEED))))
    toks = c.get("/api/tokens").json()
    assert toks and all(t["token_status"]["trading_status"] == "trading" for t in toks)
    n = StatusEx.calls
    c.get("/api/tokens")
    assert StatusEx.calls == n == len(toks)  # cached ~60 s
    plain = TestClient(create_app(s, build_components(s, exchange=MockExchangeClient(seed=SEED))))
    assert all(t["token_status"] is None for t in plain.get("/api/tokens").json())


def test_activation_defaults_to_auto_execute_unless_asked():
    c = TestClient(create_app(Settings(sim_seed=SEED)))
    assert c.get("/api/control").json()["confirmation_mode"] is False  # fresh start: autonomous
    c.post("/api/control/confirmation-mode", json={"enabled": True})
    c.post("/api/auto/activate", json={"symbol": SYM, "capital": 1000})
    assert c.get("/api/control").json()["confirmation_mode"] is False
    c.post("/api/auto/activate", json={"symbol": SYM, "capital": 1000, "activation": "CONFIRMATION"})
    assert c.get("/api/control").json()["confirmation_mode"] is True
    d = trading_strategy(SYM).model_dump(mode="json")
    d.pop("activation")
    c.post("/api/manual/activate", json={"strategy": d})
    assert c.get("/api/control").json()["confirmation_mode"] is False


def test_live_status_exposes_cooldowns_and_in_flight_orders():
    c, ex, risk, cid = inflight()
    st = c.get("/api/live/status").json()
    assert st["cooldowns"] == []
    (p,) = st["chain_pending"]
    assert p["client_order_id"] == cid and p["symbol"] == SYM and p["notional"] > 0 and p["since"] and p["held_exit"] is None
    assert c.get("/api/system").json()["live"]["chain_pending"][0]["client_order_id"] == cid


# ---------------------------------------------------------------- small-wallet follow-up: limits pass-through + min order
def test_wallet_limits_pass_through_every_live_limits_key_incl_min_wallet_usdt():
    """/api/wallet limits carries every key risk.live_limits() returns, incl. future ones (Bot 3 min_wallet_usdt)."""
    extra = {"min_wallet_usdt": 70.0, "fee_buffer_usdt": 1.5, "future_key": {"nested": [1, 2]}}

    class MinWalletRisk(ContextRisk):
        def live_limits(self, portfolio=None, context=None):
            return {**super().live_limits(portfolio, context), **extra}

    from backend.contracts import ControlState
    risk = MinWalletRisk(control=ControlState(trading_mode=TradingMode.LIVE), live_allowed=True)
    c, _ = live_client(risk=risk)
    lim = c.get("/api/wallet").json()["limits"]
    rl = risk.live_limits()
    for k, v in rl.items():
        assert k in lim, k
        if k not in ("daily_used_usdt", "daily_remaining_usdt", "warnings"):  # app-merged usage fields
            assert lim[k] == v, (k, lim[k], v)
    assert lim["min_wallet_usdt"] == 70.0


class _StatusEx(FakeLiveExchange):
    def __init__(self, status, **kw):
        super().__init__(**kw)
        self._status = status

    def get_token_status(self, symbol):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


@pytest.mark.parametrize("status,expected", [
    ({"trading_status": "TRADING", "min_order_usdt": 20, "reference_price_only": False}, 20.0),
    ({"trading_status": "TRADING", "min_order_usdt": None}, None),
    (RuntimeError("status endpoint down"), None),
])
def test_market_context_carries_token_min_order(status, expected):
    """The loop passes get_token_status(sym).min_order_usdt to engine.set_market_context (None if unavailable)."""
    c, ex = live_client(ex=_StatusEx(status))
    loop = c.app.state.st.loop
    seen = {}

    class Eng:
        def set_market_context(self, sym, ctx):
            seen[sym] = ctx

    loop.c.engine = Eng()
    loop._update_market_context(SYM, loop.book.portfolio())
    assert "min_order_usdt" in seen[SYM] and seen[SYM]["min_order_usdt"] == expected
    if not isinstance(status, Exception):
        assert seen[SYM]["status"] == "TRADING"


def test_market_context_status_enum_is_plain_string():
    """Bot 4 TokenStatus.trading_status is a str-Enum; the engine must get 'OFFHOURS', not 'TradingStatus.OFFHOURS'."""
    try:
        from backend.exchange.status import TradingStatus
    except Exception:
        pytest.skip("Bot 4 TradingStatus not importable")
    c, ex = live_client(ex=_StatusEx({"trading_status": TradingStatus.OFFHOURS, "min_order_usdt": 20.0}))
    loop, seen = c.app.state.st.loop, {}
    loop.c.engine = type("E", (), {"set_market_context": lambda self, s, ctx: seen.__setitem__(s, ctx)})()
    loop._update_market_context(SYM, loop.book.portfolio())
    assert seen[SYM]["status"] == "OFFHOURS" and seen[SYM]["min_order_usdt"] == 20.0

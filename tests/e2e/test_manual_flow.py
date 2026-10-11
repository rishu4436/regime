"""E2E Manual Mode in SIM: build -> save -> backtest -> activate -> confirm/reject -> exits -> performance."""
from __future__ import annotations

import pytest

from backend.contracts import BacktestResult, Decision, ManualStrategy, StatusSnapshot

from .helpers import make_client, run_all, tick_until, trading_strategy


def test_health_and_system_report_sim():
    c = make_client()
    assert c.get("/health").json()["status"] == "ok"
    sysinfo = c.get("/api/system").json()
    assert sysinfo["trading_mode"] == "SIM" and sysinfo["live_enabled"] is False
    assert set(sysinfo["components"]) == {"engine", "risk", "exchange", "explainer"}
    assert c.get("/api/control").json()["trading_mode"] == "SIM"
    assert {t["symbol"] for t in c.get("/api/tokens").json()} >= {"NVDAon", "TSLAx", "AAPLB"}


def test_strategy_crud_and_validation():
    c = make_client()
    s = trading_strategy().model_dump(mode="json")
    saved = c.post("/api/strategies", json=s).json()
    assert ManualStrategy.model_validate(saved).id == s["id"]
    assert [x["id"] for x in c.get("/api/strategies").json()] == [s["id"]]
    s2 = {**saved, "name": "renamed"}
    put = c.put(f"/api/strategies/{s['id']}", json=s2).json()
    assert put["name"] == "renamed" and put["version"] == saved["version"] + 1
    bad = {**s, "risk": {**s["risk"], "position_size_pct": 0}}
    assert c.post("/api/strategies/validate", json=bad).status_code == 422
    assert c.delete(f"/api/strategies/{s['id']}").json() == {"deleted": s["id"]}
    assert c.get(f"/api/strategies/{s['id']}").status_code == 404


def test_backtest_is_deterministic_and_drawdown_is_mark_to_market():
    c = make_client()
    req = {"strategy": trading_strategy().model_dump(mode="json"), "initial_capital": 10000, "limit": 400}
    a, b = c.post("/api/backtest", json=req).json(), c.post("/api/backtest", json=req).json()
    assert a == b  # seeded data -> reproducible (simulator used unseeded Math.random)
    r = BacktestResult.model_validate(a)
    assert r.num_trades > 0 and len(r.equity_curve) > 0
    curve = [p.equity for p in r.equity_curve]
    peak, mdd = r.initial_capital, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100)
    assert r.max_drawdown_pct >= mdd - 1e-6


def test_manual_confirmation_flow_approve_then_exit():
    c = make_client()
    strat = trading_strategy(activation="CONFIRMATION")
    sid = c.post("/api/strategies", json=strat.model_dump(mode="json")).json()["id"]
    st = StatusSnapshot.model_validate(c.post("/api/manual/activate", json={"strategy_id": sid}).json())
    assert st.agent_mode.value == "MANUAL" and st.active_symbol == "AAPLB" and st.control.confirmation_mode

    pending, _ = tick_until(c, lambda d: d["execution"] and d["execution"]["status"] == "PENDING_CONFIRMATION")
    assert pending is not None, "strategy never produced an entry"
    assert pending["action"] == "BUY" and pending["risk_verdict"]["approved"] and pending["explanation"]
    assert [d["id"] for d in c.get("/api/confirmations").json()] == [pending["id"]]
    assert c.get("/api/portfolio").json()["positions"] == []  # nothing executed before approval

    done = Decision.model_validate(c.post(f"/api/confirmations/{pending['id']}/approve").json())
    assert done.id == pending["id"] and done.action.value == "BUY" and done.execution.status.value == "FILLED"
    pf = c.get("/api/portfolio").json()
    assert len(pf["positions"]) == 1 and pf["cash"] < 10000
    pos = pf["positions"][0]
    assert pos["stop_loss_price"] < pos["avg_entry_price"] < pos["take_profit_price"]

    sell, _ = tick_until(c, lambda d: d["action"] == "SELL")
    assert sell is not None and sell["execution"]["status"] == "FILLED"
    assert c.get("/api/portfolio").json()["positions"] == []
    perf = c.get("/api/performance").json()
    assert perf["num_trades"] == 1 and perf["realized_pnl"] != 0
    assert perf["equity"] == c.get("/api/portfolio").json()["equity"]
    # decision log: newest first, and the approved decision replaced the pending one (same id)
    ds = c.get("/api/decisions?limit=1000").json()
    assert ds[0]["timestamp"] >= ds[-1]["timestamp"]
    assert [d for d in ds if d["id"] == pending["id"]][0]["execution"]["status"] == "FILLED"


def test_manual_reject_and_expired_confirmation():
    c = make_client(pending_ttl_seconds=0)
    c.post("/api/manual/activate", json={"strategy": trading_strategy().model_dump(mode="json")})
    p1, _ = tick_until(c, lambda d: d["execution"] and d["execution"]["status"] == "PENDING_CONFIRMATION")
    r = c.post(f"/api/confirmations/{p1['id']}/reject", json={"reason": "not now"}).json()
    assert r["action"] == "STAY_OUT" and r["execution"]["status"] == "REJECTED"
    p2, _ = tick_until(c, lambda d: d["execution"] and d["execution"]["status"] == "PENDING_CONFIRMATION")
    r2 = c.post(f"/api/confirmations/{p2['id']}/approve").json()
    assert r2["action"] == "STAY_OUT" and r2["execution"]["status"] == "EXPIRED"
    assert c.post(f"/api/confirmations/{p2['id']}/approve").status_code == 404
    assert c.get("/api/portfolio").json()["positions"] == []


def test_manual_auto_execute_session_is_reproducible():
    def session():
        c = make_client()
        c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
        ds = run_all(c)
        return c, [(d["timestamp"], d["action"], d["reason"], (d["execution"] or {}).get("avg_price")) for d in ds]

    c, a = session()
    _, b = session()
    assert a == b
    acts = {x[1] for x in a}
    assert {"BUY", "SELL", "STAY_OUT"} <= acts
    assert c.get("/api/confirmations").json() == []
    assert c.get("/api/loop").json()["replay_finished"] is True
    assert any(al["code"] == "REPLAY_FINISHED" for al in c.get("/api/alerts").json())


def test_pause_blocks_entries_but_protective_exits_still_run():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    buy, _ = tick_until(c, lambda d: d["action"] == "BUY")
    assert buy is not None
    ctrl = c.post("/api/control/pause", json={"reason": "lunch"}).json()
    assert ctrl["paused"] is True
    assert any(a["code"] == "PAUSED" for a in c.get("/api/alerts").json())
    ds = []
    for _ in range(200):
        ds += c.post("/api/loop/tick").json()
        if not c.get("/api/portfolio").json()["positions"]:
            break
    assert not [d for d in ds if d["action"] == "BUY"]
    assert [d for d in ds if d["action"] == "SELL"], "open position should still be closed by exits while paused"
    blocked = [d for d in ds if d["risk_verdict"] and not d["risk_verdict"]["approved"]]
    assert all(d["action"] == "STAY_OUT" for d in blocked)
    c.post("/api/control/resume")
    again, _ = tick_until(c, lambda d: d["action"] == "BUY")
    assert again is not None


def test_emergency_stop_flattens_and_blocks_until_reset():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    assert tick_until(c, lambda d: d["action"] == "BUY")[0] is not None
    ctrl = c.post("/api/control/emergency-stop", json={"reason": "test", "flatten": True}).json()
    assert ctrl["emergency_stopped"] is True
    assert c.get("/api/portfolio").json()["positions"] == []
    last_sell = [d for d in c.get("/api/decisions").json() if d["action"] == "SELL"][0]
    assert "EMERGENCY_STOP" in last_sell["reason"]
    assert any(a["code"] == "EMERGENCY_STOP" and a["level"] == "CRITICAL" for a in c.get("/api/alerts").json())
    for _ in range(30):
        for d in c.post("/api/loop/tick").json():
            assert d["action"] == "STAY_OUT"
    # resume() clears pause only; the emergency stop needs the explicit reset (contract v0.2)
    assert c.post("/api/control/resume").json()["emergency_stopped"] is True
    for _ in range(5):
        for d in c.post("/api/loop/tick").json():
            assert d["action"] == "STAY_OUT"
    ctrl = c.post("/api/control/reset-emergency-stop").json()
    assert ctrl["emergency_stopped"] is False and ctrl["paused"] is False
    assert tick_until(c, lambda d: d["action"] == "BUY")[0] is not None


def test_manual_close_override_and_errors():
    c = make_client()
    assert c.post("/api/loop/tick").status_code == 409  # no active agent
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    assert tick_until(c, lambda d: d["action"] == "BUY")[0] is not None
    # switching symbol with an open position is refused
    other = trading_strategy(symbol="NVDAon").model_dump(mode="json")
    assert c.post("/api/manual/activate", json={"strategy": other}).status_code == 409
    d = c.post("/api/positions/AAPLB/close").json()
    assert d["action"] == "SELL" and "MANUAL" in d["reason"]
    assert c.post("/api/positions/AAPLB/close").status_code == 404
    assert c.post("/api/manual/activate", json={"strategy": other}).status_code == 200


def _app_loop(c):
    return c.app.state.st.loop


def test_loop_raises_highest_price_from_bar_high_after_check_exits():
    c = make_client()
    strat = trading_strategy(activation="AUTO_EXECUTE")
    d = strat.model_dump(mode="json")
    d["exit"].update(take_profit_pct=50.0, stop_loss_pct=40.0)  # keep the lot open for a while
    d["exit"]["rsi_above"]["enabled"] = False
    c.post("/api/manual/activate", json={"strategy": d})
    buy, _ = tick_until(c, lambda x: x["action"] == "BUY")
    assert buy is not None
    loop = _app_loop(c)
    series = loop.feed.series("AAPLB", strat.timeframe)
    entry_i = next(i for i, x in enumerate(series) if x.timestamp == loop.book.lots[0].position.opened_at)
    seen_exit_inputs = []
    real = loop.c.risk.check_exits

    def spy(portfolio, prices, bars=None):
        seen_exit_inputs.append(portfolio.positions[0].highest_price if portfolio.positions else None)
        return real(portfolio, prices, bars=bars)

    loop.c.risk.check_exits = spy
    try:
        for k in range(1, 15):
            c.post("/api/loop/tick")
            pos = c.get("/api/portfolio").json()["positions"][0]
            highs = [x.high for x in series[entry_i + 1:entry_i + 1 + k]]
            # after the tick: raised with this bar's high; during check_exits: only PRIOR bars' highs
            assert pos["highest_price"] == pytest.approx(max([pos["avg_entry_price"]] + highs))
            assert seen_exit_inputs[-1] == pytest.approx(max([pos["avg_entry_price"]] + highs[:-1]))
    finally:
        loop.c.risk.check_exits = real


def test_close_single_lot_by_position_id():
    c = make_client()
    d = trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")
    d["risk"].update(max_open_positions=2, cooldown_bars=0)
    d["exit"].update(take_profit_pct=50.0, stop_loss_pct=40.0)
    d["exit"]["rsi_above"]["enabled"] = False
    c.post("/api/manual/activate", json={"strategy": d})
    for _ in range(400):
        c.post("/api/loop/tick")
        if len(c.get("/api/portfolio").json()["positions"]) == 2:
            break
    lots = c.get("/api/portfolio").json()["positions"]
    assert len(lots) == 2 and lots[0]["id"] != lots[1]["id"]
    r = c.post(f"/api/positions/by-id/{lots[1]['id']}/close")
    assert r.status_code == 200
    dec = r.json()
    assert dec["action"] == "SELL" and dec["signal"]["exit_reason"] == "MANUAL" and dec["signal"]["position_id"] == lots[1]["id"]
    left = c.get("/api/portfolio").json()["positions"]
    assert [p["id"] for p in left] == [lots[0]["id"]]
    assert c.post(f"/api/positions/by-id/{lots[1]['id']}/close").status_code == 404
    assert c.get("/api/performance").json()["num_trades"] == 1

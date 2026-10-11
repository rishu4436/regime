"""Regressions from the strategy QA run (qa-strategies/RESULTS.md): F1 multi-lot engine exits, F2 confirmation toggle vs
the active strategy's activation, F3 duplicate pending confirmations. SIM only, seeded mocks."""
from __future__ import annotations

import pytest

from .helpers import make_client


# ---------------------------------------------------------------- QA run (qa-strategies) F1 / F2 / F3
def _always_in(max_pos=1, activation="AUTO_EXECUTE", cooldown=0, rsi_exit=None, tp=50.0, sl=50.0) -> dict:
    from backend.contracts import ManualStrategy
    s = ManualStrategy(name="qa", symbol="AAPLB").model_dump(mode="json")
    for v in s["entry"].values():
        if isinstance(v, dict) and "enabled" in v:
            v["enabled"] = False
    s["entry"]["rsi_below"].update(enabled=True, threshold=99)  # (almost) always enter
    s["exit"].update(take_profit_pct=tp, stop_loss_pct=sl)
    s["exit"]["trailing_stop"]["enabled"] = False
    s["exit"]["macd_bearish_cross"]["enabled"] = False
    s["exit"]["rsi_above"].update(enabled=rsi_exit is not None, threshold=rsi_exit or 70)
    s["risk"].update(max_open_positions=max_pos, cooldown_bars=cooldown, position_size_pct=10)
    s["activation"] = activation
    return s


def test_f1_multi_lot_engine_exit_logs_one_decision_per_lot_and_replays_to_book():
    """F1: with max_positions=3 an RSI exit used to sell all lots but log one decision (last lot's fill only)."""
    c = make_client()
    assert c.post("/api/manual/activate", json={"strategy": _always_in(max_pos=3, rsi_exit=70)}).status_code == 200
    c.post("/api/control/confirmation-mode", json={"enabled": False})
    ds = []
    for _ in range(120):
        ds += c.post("/api/loop/tick").json()
    pf, perf = c.get("/api/portfolio").json(), c.get("/api/performance").json()
    cash, qty, sells = pf["starting_capital"], 0.0, 0
    for d in ds:
        e = d.get("execution") or {}
        if e.get("status") != "FILLED":
            continue
        if d["action"] == "BUY":
            cash -= e["notional"] + e["fee"]
            qty += e["filled_quantity"]
        elif d["action"] == "SELL":
            cash += e["notional"] - e["fee"]
            qty -= e["filled_quantity"]
            sells += 1
            if d["signal"].get("position_id"):  # the decision's execution is the targeted lot's fill
                assert d["execution"]["filled_quantity"] > 0
    assert sells >= 2, "scenario must exercise exits"
    assert cash == pytest.approx(pf["cash"], abs=1e-6)
    assert qty == pytest.approx(sum(p["quantity"] for p in pf["positions"]), abs=1e-6)
    assert perf["num_trades"] == sells


def test_f3_no_duplicate_pending_confirmation_per_symbol():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": _always_in(max_pos=1, activation="CONFIRMATION", tp=1.0, sl=1.0)})
    ds = []
    for _ in range(3):
        ds += c.post("/api/loop/tick").json()
    pend = c.get("/api/confirmations").json()
    assert len(pend) == 1, [p["reason"] for p in pend]
    waits = [d for d in ds if d["action"] == "STAY_OUT" and "awaiting your approval" in d["reason"]]
    assert waits and pend[0]["id"] in waits[0]["reason"]
    a = c.post(f"/api/confirmations/{pend[0]['id']}/approve").json()
    assert a["execution"]["status"] == "FILLED" and not c.get("/api/confirmations").json()


def test_f2_confirmation_toggle_updates_active_activation():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": _always_in(max_pos=1, activation="CONFIRMATION", tp=1.0, sl=1.0)})
    loop = c.app.state.st.loop
    r = c.post("/api/control/confirmation-mode", json={"enabled": False}).json()
    assert r["confirmation_mode"] is False and loop.manual.activation.value == "AUTO_EXECUTE"
    buys = []
    for _ in range(5):
        buys += [d for d in c.post("/api/loop/tick").json() if d["action"] == "BUY"]
        if buys:
            break
    assert buys and buys[0]["execution"]["status"] == "FILLED", buys and buys[0]["reason"]
    assert not c.get("/api/confirmations").json()
    r = c.post("/api/control/confirmation-mode", json={"enabled": True}).json()
    assert r["confirmation_mode"] is True and loop.manual.activation.value == "CONFIRMATION"
    assert loop.state.get("agent")["strategy"]["activation"] == "CONFIRMATION"  # survives a restart


def test_f2_toggle_updates_auto_activation():
    c = make_client()
    c.post("/api/auto/activate", json={"symbol": "AAPLB", "capital": 1000, "activation": "CONFIRMATION"})
    loop = c.app.state.st.loop
    c.post("/api/control/confirmation-mode", json={"enabled": False})
    assert loop.auto.activation.value == "AUTO_EXECUTE"
    c.post("/api/control/confirmation-mode", json={"enabled": True})
    assert loop.auto.activation.value == "CONFIRMATION"

"""E2E Auto Mode in SIM: regime -> map(regime, style) -> exactly one preset -> risk -> BUY / STAY OUT with reason."""
from __future__ import annotations

import pytest

from backend.contracts import AutoConfig, PresetName, Regime, StrategySelection, TraderStyle

from .helpers import make_client, run_all, tick_until

PRESETS = {p.value for p in PresetName}


def _cfg(symbol: str, style: str = "BALANCED", activation: str = "AUTO_EXECUTE", **kw) -> dict:
    return AutoConfig(symbol=symbol, style=TraderStyle(style), capital=5000, activation=activation, **kw).model_dump(mode="json")


@pytest.mark.parametrize("symbol", ["NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"])
def test_regime_and_selection_endpoints(symbol):
    c = make_client()
    ra = c.get(f"/api/regime/{symbol}").json()
    assert ra["regime"] in {r.value for r in Regime} and ra["reason"]
    sel = StrategySelection.model_validate(c.get(f"/api/auto/selection?symbol={symbol}&style=BALANCED").json())
    assert sel.regime.value == ra["regime"]
    assert sel.preset is None or sel.preset in PresetName


@pytest.mark.parametrize("symbol,style", [("AAPLB", "PATIENT"), ("NVDAon", "ACTIVE"), ("SPYon", "BALANCED"), ("METAx", "BALANCED"), ("TSLAx", "BALANCED")])
def test_auto_session_single_strategy_per_decision(symbol, style):
    c = make_client()
    st = c.post("/api/auto/activate", json=_cfg(symbol, style)).json()
    assert st["agent_mode"] == "AUTO" and st["control"]["confirmation_mode"] is False
    ds = run_all(c)
    assert ds
    for d in ds:
        assert d["agent_mode"] == "AUTO" and d["mode"] == "SIM"
        assert d["reason"] and d["explanation"]
        assert d["action"] in ("BUY", "SELL", "STAY_OUT")
        if d["action"] == "BUY":
            assert d["regime"] is not None
            assert d["strategy"] in PRESETS  # exactly one preset
            assert d["risk_verdict"]["approved"] and d["execution"]["status"] == "FILLED"
    st = c.get("/api/status").json()
    assert st["current_regime"] is not None
    assert st["active_strategy"] in PRESETS | {"NONE"}


def test_patient_sideways_dca_buys():
    c = make_client()
    c.post("/api/auto/activate", json=_cfg("AAPLB", "PATIENT"))
    buy, _ = tick_until(c, lambda d: d["action"] == "BUY")
    assert buy is not None and buy["strategy"] in PRESETS


def test_auto_session_is_reproducible():
    def run():
        c = make_client()
        c.post("/api/auto/activate", json=_cfg("METAx", "ACTIVE"))
        return [(d["timestamp"], d["action"], d["strategy"], d["regime"], d["reason"]) for d in run_all(c)]

    assert run() == run()


def test_auto_confirmation_mode_and_ws_events():
    c = make_client()
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "STATUS" and first["payload"]["control"]["trading_mode"] == "SIM"
        c.post("/api/auto/activate", json=_cfg("AAPLB", "PATIENT", activation="CONFIRMATION"))
        pending, _ = tick_until(c, lambda d: d["execution"] and d["execution"]["status"] == "PENDING_CONFIRMATION")
        assert pending is not None
        c.post(f"/api/confirmations/{pending['id']}/approve")
        seen: dict[str, list] = {}
        for _ in range(2000):
            ev = ws.receive_json()
            seen.setdefault(ev["type"], []).append(ev["payload"])
            if ev["type"] == "EXECUTION" and ev["payload"].get("decision_id") == pending["id"]:
                break
        assert {"CONTROL", "STATUS", "PRICE", "DECISION", "EXECUTION"} <= set(seen)
        assert any(p["id"] == pending["id"] and p["execution"]["status"] == "PENDING_CONFIRMATION" for p in seen["DECISION"])
        assert seen["EXECUTION"][-1]["status"] == "FILLED"
        ws.send_json({"type": "ping"})
        for _ in range(50):
            ev = ws.receive_json()
            if ev["type"] == "STATUS" and ev["payload"]["portfolio"]["positions"]:
                break
        assert ev["type"] == "STATUS"


def test_disabled_preset_means_stay_out():
    c = make_client()
    sel = c.get("/api/auto/selection?symbol=AAPLB&style=PATIENT").json()
    if sel["preset"] is None:
        pytest.skip("mapping already stays out")
    enabled = [p.value for p in PresetName if p.value != sel["preset"]]
    c.post("/api/auto/activate", json=_cfg("AAPLB", "PATIENT", enabled_presets=enabled))
    ds = run_all(c, max_ticks=60)
    for d in ds:
        assert d["strategy"] != sel["preset"] or d["action"] == "STAY_OUT"


def test_llm_endpoints_offline():
    c = make_client()
    r = c.post("/api/llm/parse", json={"text": "buy NVDAon when RSI below 30, take profit 8%, stop loss 4%"}).json()
    assert r["kind"] in ("MANUAL", "AUTO")
    assert c.post("/api/llm/ask", json={"question": "what is the agent doing?"}).json()["answer"]
    assert c.get("/api/llm/summary").json()["summary"]


def test_presets_table_covers_every_regime_and_style():
    c = make_client()
    t = c.get("/api/auto/presets-table").json()
    assert t["source"].split(":")[0] in ("engine", "fallback")
    keys = {(e["regime"], e["style"]) for e in t["entries"]}
    assert keys == {(r.value, s.value) for r in Regime for s in TraderStyle}
    for e in t["entries"]:
        assert e["preset"] is None or e["preset"] in PRESETS
        assert 0 <= e["size_multiplier"] <= 1 and e["reason"]
    # consistent with the single-combination preview endpoint
    sel = c.get("/api/auto/selection?symbol=AAPLB&style=PATIENT").json()
    row = next(e for e in t["entries"] if e["regime"] == sel["regime"] and e["style"] == "PATIENT")
    assert row["preset"] == sel["preset"]

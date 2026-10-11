"""Dashboard "Save strategy" (Rishu's 'saved but never appears' bug, 11 Oct): the exact bodies the frontend sends
(tests/e2e/fixtures/dashboard_strategy_bodies.json) must save (200) and be listed, survive a backend restart when
LOG_DIR is set (run.sh / run.ps1 always set it), and a stale id from localStorage must 404 on PUT (the dashboard then
saves it as new, Bot 1 d958372) without hiding the fresh save. Also: the loop's daily LIVE spend is gross (fee incl.)."""
from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.contracts import ExecutionPath, ExecutionResult, OrderSide, OrderStatus, TradingMode

from .helpers import make_client

BODIES = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "dashboard_strategy_bodies.json")))
STALE = "0123456789abcdef0123456789abcdef"


def _save_both(c):
    ids = []
    for k in ("dashboard_default", "parser_filled"):
        r = c.post("/api/strategies", json=BODIES[k])
        assert r.status_code == 200, (k, r.text)
        ids.append(r.json()["id"])
    return ids


def test_dashboard_bodies_save_and_list():
    c = make_client()
    ids = _save_both(c)
    listed = c.get("/api/strategies").json()
    assert [s["id"] for s in listed] == ids
    assert {s["name"] for s in listed} == {BODIES["dashboard_default"]["name"], BODIES["parser_filled"]["name"]}
    # and it activates + buys without approval from the saved id
    r = c.post("/api/manual/activate", json={"strategy_id": ids[0]})
    assert r.status_code == 200 and r.json()["control"]["confirmation_mode"] is False


def test_stale_id_put_404_then_fresh_save_is_listed():
    c = make_client()
    body = {**BODIES["dashboard_default"], "id": STALE}
    r = c.put(f"/api/strategies/{STALE}", json=body)
    assert r.status_code == 404 and STALE in r.json()["detail"]
    assert c.get("/api/strategies").json() == []
    r = c.post("/api/strategies", json=BODIES["dashboard_default"])  # what the dashboard falls back to
    assert r.status_code == 200
    assert [s["id"] for s in c.get("/api/strategies").json()] == [r.json()["id"]]
    # updating the real id works and bumps the version
    sid = r.json()["id"]
    u = c.put(f"/api/strategies/{sid}", json={**BODIES["dashboard_default"], "id": sid, "name": "renamed"})
    assert u.status_code == 200 and u.json()["version"] == 2 and u.json()["name"] == "renamed"


def test_saved_strategies_survive_restart_with_log_dir(tmp_path):
    s = Settings(log_dir=str(tmp_path))
    c = TestClient(create_app(s))
    ids = _save_both(c)
    assert (tmp_path / "strategies.json").exists()
    c2 = TestClient(create_app(Settings(log_dir=str(tmp_path))))
    assert [x["id"] for x in c2.get("/api/strategies").json()] == ids


def test_bad_body_returns_readable_422():
    c = make_client()
    bad = json.loads(json.dumps(BODIES["dashboard_default"]))
    bad["exit"]["take_profit_pct"] = 0
    r = c.post("/api/strategies", json=bad)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"][-1] == "take_profit_pct"


def test_daily_live_spend_counts_fee():
    c = make_client()
    L = c.app.state.st.loop
    res = ExecutionResult(client_order_id="x", symbol="NVDAB", side=OrderSide.BUY, status=OrderStatus.FILLED,
                          path=ExecutionPath.SWAP, mode=TradingMode.LIVE, filled_quantity=0.04, avg_price=230.0,
                          notional=10.0, fee=0.02)
    L._record_live_buy(res)
    L._record_live_buy(res)
    assert abs(L.daily_live_notional() - 20.04) < 1e-9

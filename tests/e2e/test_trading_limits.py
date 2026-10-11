"""Trading limits from the dashboard (GET/PUT /api/settings/limits, POST .../reset). No network, no real baw, no keys.

- localhost-only (403 for a LAN client / forwarded / LAN Origin), like wallet sign-in
- validation by risk (Bot 3): plain 422s (non-positive, per-order above total, pct out of (0, 100], per-order above the
  wallet's own daily limit); order_mode 'strategy' (default, null) with null sizes always succeeds
- persistence in LOG_DIR/limits.json across an app restart; reset restores the env defaults and deletes the file
- a fake LIVE 10,000 USDT wallet: 25 USDT per order by default, 200 after PUT (fixed 200, or 2% of the wallet)
- the wallet's own daily quota stays the final cap; a limits change never clears an emergency stop
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.contracts import TradingMode

from . import test_small_wallet_live as sw
from .helpers import SEED

LOCAL = ("127.0.0.1", 50000)
BIG = {"order_mode": "fixed", "order_usdt": 200, "order_pct": None, "max_order_usdt": 200, "max_total_usdt": 1000,
       "max_per_token_usdt": 500, "daily_loss_usdt": 100}
PCT = {**BIG, "order_mode": "pct", "order_usdt": None, "order_pct": 2}


def _local(app) -> TestClient:
    return TestClient(app, client=LOCAL, base_url="http://localhost:8000")


def _sim(tmp_path) -> TestClient:
    s = Settings(trading_mode=TradingMode.SIM, sim_seed=SEED, log_dir=str(tmp_path), state_dir=str(tmp_path))
    return _local(create_app(s))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "XAI_API_KEY", "RISK_LIVE_MAX_ORDER_USDT", "RISK_LIVE_MAX_DAILY_LOSS_USDT",
              "RISK_LIVE_MAX_DAILY_NOTIONAL_USDT", "RISK_LIVE_MAX_EXPOSURE_USDT"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_shape(tmp_path):
    r = _sim(tmp_path).get("/api/settings/limits")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["defaults"] == {"order_mode": "strategy", "order_usdt": None, "order_pct": None, "max_order_usdt": 25.0,
                             "max_total_usdt": 50.0, "max_per_token_usdt": 50.0, "daily_loss_usdt": 25.0,
                             "daily_buy_usdt": 100.0}
    assert {k: d[k] for k in d["defaults"]} == d["defaults"]
    assert d["wallet_daily_limit_usdt"] is None and d["is_default"] is True


def test_localhost_only(tmp_path):
    c = _sim(tmp_path)
    lan = TestClient(c.app, client=("192.168.1.20", 4000))
    assert lan.get("/api/settings/limits").status_code == 403
    r = lan.put("/api/settings/limits", json=BIG)
    assert r.status_code == 403 and "only be changed on the PC" in r.json()["detail"]
    assert lan.post("/api/settings/limits/reset").status_code == 403
    assert c.put("/api/settings/limits", json=BIG, headers={"X-Forwarded-For": "192.168.1.20"}).status_code == 403
    assert c.put("/api/settings/limits", json=BIG, headers={"Origin": "http://192.168.1.20:5173"}).status_code == 403
    assert c.get("/api/settings/limits").json()["is_default"] is True  # nothing changed
    assert c.put("/api/settings/limits", json=BIG, headers={"Origin": "http://localhost:5173"}).status_code == 200


@pytest.mark.parametrize("body,needle", [
    ({"max_order_usdt": 0}, "Max per order must be greater than 0"),
    ({"daily_loss_usdt": -5}, "Daily loss limit must be greater than 0"),
    ({"max_total_usdt": "lots"}, "Max total invested must be a number"),
    ({"order_mode": "pct", "order_pct": 0}, "must be greater than 0"),
    ({"order_mode": "pct", "order_pct": 150}, "at most 100"),
    ({"order_mode": "yolo"}, "Order size mode must be"),
    ({**BIG, "max_order_usdt": 2000, "order_usdt": 100}, "can't be above Max total invested"),
    ({**BIG, "max_per_token_usdt": 2000}, "Max in one token"),
    ({**BIG, "order_usdt": 300}, "can't be above Max per order"),
    ({"bogus": 1}, "Unknown setting"),
])
def test_validation_422(tmp_path, body, needle):
    c = _sim(tmp_path)
    r = c.put("/api/settings/limits", json=body)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]
    assert c.get("/api/settings/limits").json()["is_default"] is True
    assert not os.path.exists(tmp_path / "limits.json")


def test_strategy_mode_with_nulls_always_succeeds(tmp_path):
    c = _sim(tmp_path)
    assert c.put("/api/settings/limits", json=BIG).status_code == 200
    r = c.put("/api/settings/limits", json={"order_mode": "strategy", "order_usdt": None, "order_pct": None})
    assert r.status_code == 200, r.text
    assert (r.json()["order_mode"], r.json()["order_usdt"], r.json()["order_pct"]) == ("strategy", None, None)
    r = c.put("/api/settings/limits", json={"order_mode": None})
    assert r.status_code == 200 and r.json()["order_mode"] == "strategy"


def test_persists_across_restart_and_reset(tmp_path):
    c = _sim(tmp_path)
    r = c.put("/api/settings/limits", json=PCT)
    assert r.status_code == 200, r.text
    assert r.json()["order_mode"] == "pct" and r.json()["max_order_usdt"] == 200 and r.json()["is_default"] is False
    saved = json.loads((tmp_path / "limits.json").read_text())
    assert saved["max_per_token_usdt"] == 500 and saved["order_pct"] == 2 and saved["order_mode"] == "pct"
    alerts = [a for a in c.get("/api/alerts").json() if a["code"] == "LIMITS_CHANGED"]
    assert alerts and "2% of the wallet" in alerts[0]["message"]
    assert "LIMITS_CHANGED" in (tmp_path / "events.jsonl").read_text()

    c2 = _sim(tmp_path)  # restart
    d = c2.get("/api/settings/limits").json()
    assert d["max_order_usdt"] == 200 and d["order_mode"] == "pct" and d["is_default"] is False
    risk = c2.app.state.st.loop.c.risk
    live = getattr(getattr(risk, "config", None), "live", None)
    if live is not None:  # Bot 3's LiveRiskProfile got the restored values
        assert live.max_order_notional_usdt == 200 and live.max_open_exposure_usdt == 1000
    assert c2.app.state.st.settings.live_max_order_usdt == 200
    ex = c2.app.state.st.loop.c.exchange
    if callable(getattr(ex, "live_limits", None)):  # Bot 4: per-order cap + exit cap that fits a full token position
        assert ex.live_limits()["max_order_usdt"] == 200 and ex.live_limits()["max_sell_usdt"] == 500
    eng = c2.app.state.st.loop.c.engine
    eng = getattr(eng, "_primary", None) or eng
    if hasattr(eng, "set_live_limits"):  # Bot 2's engine follows the same per-order cap
        assert eng.live_config.max_order_usdt == 200

    r = c2.post("/api/settings/limits/reset")
    assert r.status_code == 200 and r.json()["is_default"] is True and r.json()["max_order_usdt"] == 25
    assert r.json()["order_mode"] == "strategy"
    assert not (tmp_path / "limits.json").exists()
    assert c2.app.state.st.settings.live_max_order_usdt == 25
    assert _sim(tmp_path).get("/api/settings/limits").json()["is_default"] is True

    # a PUT with the default values is a reset too (that's what the UI's "Reset to safe defaults" sends)
    c2.put("/api/settings/limits", json=BIG)
    assert c2.put("/api/settings/limits", json=d["defaults"]).json()["is_default"] is True
    assert not (tmp_path / "limits.json").exists()


def test_limits_change_never_clears_emergency_stop(tmp_path):
    c = _sim(tmp_path)
    assert c.post("/api/control/emergency-stop", json={"reason": "Rishu 11:33"}).json()["emergency_stopped"] is True
    c.put("/api/settings/limits", json=BIG)
    c.post("/api/settings/limits/reset")
    ctl = c.get("/api/control").json()
    assert ctl["emergency_stopped"] is True and "Rishu" in (ctl.get("reason") or "")
    assert _sim(tmp_path).get("/api/control").json()["emergency_stopped"] is True  # restart: still stopped


# ---------------------------------------------------------------- fake LIVE wallet (Bot 4 SIM client reporting LIVE)
needs_live = pytest.mark.skipif(sw.SimClient is None, reason="real engine/risk/SIM client not importable")


def _live(monkeypatch, tmp_path, usdt=10_000.0, daily_limit=5000.0, used_today=0.0):
    """The SIM client's wallet settings (dailyLimit / quotaUsed / quotaLeft) set for a real wallet configured for
    bigger trades (Bot 4's SIM default dailyLimit is 100)."""
    from backend.exchange import wallet as wmod
    monkeypatch.setattr(wmod, "SIM_DAILY_LIMIT_USDT", float(daily_limit))
    c, ex = sw._client(usdt, sw.WEEKDAY, monkeypatch)
    if used_today:
        real = ex._sim_used_today
        monkeypatch.setattr(ex, "_sim_used_today", lambda: real() + used_today)
    c.app.state.st.loop.wallet_settings.get(force=True)  # the scheduled read (tests: now)
    return _local(c.app), ex


def _buy_notionals(c, ex, symbol="NVDAB", ticks=3) -> list[float]:
    ds = sw._run(c, ex, symbol, ticks)
    sw._never_failed(c, ex, ds)
    return [ex.order_notional[d["execution"]["client_order_id"]] for d in sw._filled_buys(ds)]


@needs_live
def test_live_default_is_25_per_order(monkeypatch, tmp_path):
    c, ex = _live(monkeypatch, tmp_path)
    assert c.get("/api/settings/limits").json()["wallet_daily_limit_usdt"] == 5000
    ns = _buy_notionals(c, ex)
    assert ns and max(ns) <= 25 + 1e-6, ns


@needs_live
@pytest.mark.parametrize("body", [BIG, PCT])
def test_live_10k_wallet_goes_to_200_per_order(monkeypatch, tmp_path, body):
    c, ex = _live(monkeypatch, tmp_path)
    r = c.put("/api/settings/limits", json=body)
    assert r.status_code == 200, r.text
    assert c.get("/api/wallet").json()["limits"]["per_order_cap_usdt"] == 200
    ns = _buy_notionals(c, ex)
    assert ns, "no BUY filled"
    assert max(ns) == pytest.approx(200, abs=1.0), ns
    assert all(n <= 200 + 1e-6 for n in ns)


@needs_live
def test_wallet_daily_limit_rejects_higher_caps(monkeypatch, tmp_path):
    c, ex = _live(monkeypatch, tmp_path, daily_limit=500)
    assert c.get("/api/settings/limits").json()["wallet_daily_limit_usdt"] == 500
    r = c.put("/api/settings/limits", json={**BIG, "max_order_usdt": 600, "order_usdt": 600})
    assert r.status_code == 422 and "wallet's own daily limit" in r.json()["detail"]
    assert c.get("/api/settings/limits").json()["is_default"] is True
    assert c.put("/api/settings/limits", json=BIG).status_code == 200


@needs_live
def test_wallet_quota_stays_the_final_cap(monkeypatch, tmp_path):
    c, ex = _live(monkeypatch, tmp_path, daily_limit=1000, used_today=920)
    assert c.put("/api/settings/limits", json=BIG).status_code == 200
    ds = sw._run(c, ex, "NVDAB", 3)
    sw._never_failed(c, ex, ds)
    for d in sw._filled_buys(ds):  # never above what the wallet itself still allows today
        assert ex.order_notional[d["execution"]["client_order_id"]] <= 80 + 1e-6
    for d in ds:
        if d["action"] != "BUY" and (d.get("signal") or {}).get("action") == "BUY":
            assert d["reason"].strip()


def test_exchange_refusal_changes_nothing(tmp_path, monkeypatch):
    c = _sim(tmp_path)
    ex = c.app.state.st.loop.c.exchange
    if not callable(getattr(ex, "set_live_limits", None)):
        pytest.skip("exchange without set_live_limits (Bot 4 8dd430b)")
    monkeypatch.setenv("BINANCE_LIVE_HARD_MAX_ORDER_USDT", "100")
    r = c.put("/api/settings/limits", json=BIG)
    assert r.status_code == 422, r.text
    d = c.get("/api/settings/limits").json()
    assert d["is_default"] is True and d["max_order_usdt"] == 25
    assert not (tmp_path / "limits.json").exists()
    assert ex.live_limits()["max_order_usdt"] == 25

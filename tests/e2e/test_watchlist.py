"""Multi-token watchlist + Auto-pick in SIM (Bot 4 SIM client, real engine/risk, no keys, no network).

- single-symbol configs behave exactly as before (watchlist == [symbol], one decision per tick)
- a 3-token manual watchlist buys on different tokens; one decision per token per tick; caps hold across tokens
- Auto-pick picks <= max_tokens, the watchlist follows the picks, /api/autopick previews with explanations
- a held token dropped from the watchlist still gets its exits (and never a new BUY)
- /api/tokens rows carry narrative codes; ?all=1 uses Bot 4's list_tokens()
"""
from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.contracts import TradingMode

from .helpers import SEED, trading_strategy

WATCH = ["NVDAB", "AAPLB", "NVDAon"]
NARRATIVES = {"AI_CHIPS", "EV_ENERGY", "BIG_TECH", "CRYPTO", "other"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def _sim(tmp_path=None) -> TestClient:
    s = Settings(trading_mode=TradingMode.SIM, sim_seed=SEED, log_dir=str(tmp_path) if tmp_path else None)
    return TestClient(create_app(s))


def _eager(symbol: str, watchlist=None, **exit_kw) -> dict:
    d = trading_strategy(symbol, activation="AUTO_EXECUTE").model_dump(mode="json")
    d["entry"]["rsi_below"].update(enabled=True, threshold=100)  # enter on the first ready bar
    d["risk"]["position_size_pct"] = 10.0
    d["exit"].update(**exit_kw)
    if watchlist is not None:
        d["watchlist"] = watchlist
    return d


def _ticks(c: TestClient, n: int) -> list[list[dict]]:
    return [c.post("/api/loop/tick").json() for _ in range(n)]


def test_single_symbol_unchanged():
    c = _sim()
    r = c.post("/api/manual/activate", json={"strategy": _eager("NVDAB", take_profit_pct=50, stop_loss_pct=20)})
    assert r.status_code == 200, r.text
    w = c.get("/api/watchlist").json()
    assert w["watchlist"] == ["NVDAB"] and w["auto_pick"] is None and w["picks"] == []
    for ds in _ticks(c, 5):
        assert {d["symbol"] for d in ds} <= {"NVDAB"}
        assert len([d for d in ds if d["action"] in ("STAY_OUT", "BUY")]) <= 1
    assert c.get("/api/loop").json()["active_symbol"] == "NVDAB"


def test_watchlist_buys_on_several_tokens_one_line_per_token_and_caps_hold():
    c = _sim()
    r = c.post("/api/manual/activate", json={"strategy": _eager("NVDAB", WATCH, take_profit_pct=50, stop_loss_pct=20)})
    assert r.status_code == 200, r.text
    assert c.get("/api/watchlist").json()["watchlist"] == WATCH
    ticks = _ticks(c, 6)
    for ds in ticks:  # one entry decision per token per tick (no duplicate spam)
        cnt = Counter(d["symbol"] for d in ds if d["action"] in ("STAY_OUT", "BUY"))
        assert all(v == 1 for v in cnt.values()), cnt
        assert set(cnt) <= set(WATCH)
    bought = {d["symbol"] for ds in ticks for d in ds if d["action"] == "BUY"}
    assert len(bought) >= 2, bought
    pf = c.get("/api/portfolio").json()
    exposure = sum(p["quantity"] * (p.get("current_price") or p["avg_entry_price"]) for p in pf["positions"])
    assert exposure <= 0.60 * pf["equity"] + 1e-6  # risk's total exposure cap counts every token together
    assert len({p["symbol"] for p in pf["positions"]}) <= 3
    per = {r["symbol"]: r for r in c.get("/api/watchlist").json()["per_token"]}
    assert set(WATCH) <= set(per) and any(r["held"] for r in per.values())


def test_two_buys_same_bar_respect_total_cap():
    c = _sim()
    big = _eager("NVDAB", WATCH, take_profit_pct=50, stop_loss_pct=20)
    big["risk"]["position_size_pct"] = 40.0  # 3 x 40% would be 120% of equity
    assert c.post("/api/manual/activate", json={"strategy": big}).status_code == 200
    _ticks(c, 4)
    pf = c.get("/api/portfolio").json()
    exposure = sum(p["quantity"] * (p.get("current_price") or p["avg_entry_price"]) for p in pf["positions"])
    assert exposure <= 0.60 * pf["equity"] + 1e-6
    assert pf["cash"] >= -1e-6


def test_dropped_token_still_exits():
    c = _sim()
    s = _eager("NVDAB", ["NVDAB", "AAPLB"], take_profit_pct=0.3, stop_loss_pct=0.3)
    assert c.post("/api/manual/activate", json={"strategy": s}).status_code == 200
    held = None
    for _ in range(10):
        _ticks(c, 1)
        syms = {p["symbol"] for p in c.get("/api/portfolio").json()["positions"]}
        if "AAPLB" in syms:
            held = True
            break
    assert held, "AAPLB never bought"
    s2 = _eager("NVDAB", ["NVDAB", "TSLAon"], take_profit_pct=0.3, stop_loss_pct=0.3)
    r = c.post("/api/manual/activate", json={"strategy": s2})
    assert r.status_code == 200, r.text  # switching the watchlist keeps the AAPLB position
    w = c.get("/api/watchlist").json()
    assert "AAPLB" not in w["watchlist"] and any(t["symbol"] == "AAPLB" and t["held"] and not t["watched"] for t in w["per_token"])
    exits = []
    for ds in _ticks(c, 40):
        assert not [d for d in ds if d["symbol"] == "AAPLB" and d["action"] == "BUY"]
        exits += [d for d in ds if d["symbol"] == "AAPLB" and d["action"] == "SELL"]
        if exits:
            break
    assert exits, "no exit on the dropped token"


def test_auto_pick_picks_at_most_max_tokens():
    c = _sim()
    cfg = {"symbol": "NVDAB", "capital": 1000, "activation": "AUTO_EXECUTE",
           "auto_pick": {"enabled": True, "max_tokens": 2, "narrative": "ANY"}}
    r = c.post("/api/auto/activate", json=cfg)
    assert r.status_code == 200, r.text
    w = c.get("/api/watchlist").json()
    assert 1 <= len(w["picks"]) <= 2, w
    assert w["watchlist"] == [p["symbol"] for p in w["picks"]]
    for p in w["picks"]:
        assert {"symbol", "narrative", "score", "reason"} <= set(p)
    assert w["explanations"]
    for ds in _ticks(c, 3):
        assert {d["symbol"] for d in ds} <= set(w["watchlist"]) | {p["symbol"] for p in c.get("/api/portfolio").json()["positions"]}
    prev = c.get("/api/autopick", params={"narrative": "AI_CHIPS", "max_tokens": 3}).json()
    assert len(prev["picks"]) <= 3 and all(p["narrative"] == "AI_CHIPS" for p in prev["picks"])
    assert "skipped" in prev and "explanations" in prev
    assert c.get("/api/autopick", params={"narrative": "MEME"}).status_code == 422


def test_watchlist_contract_validation():
    c = _sim()
    bad = _eager("NVDAB", ["A", "B", "C", "D", "E", "F"])
    assert c.post("/api/manual/activate", json={"strategy": bad}).status_code == 422
    cfg = {"symbol": "NVDAB", "capital": 1000, "auto_pick": {"enabled": True, "max_tokens": 9, "narrative": "ANY"}}
    assert c.post("/api/auto/activate", json=cfg).status_code == 422


def test_tokens_rows_have_narrative_codes():
    c = _sim()
    rows = c.get("/api/tokens").json()
    assert rows and all(r["narrative"] in NARRATIVES for r in rows)
    nv = next(r for r in rows if r["symbol"] == "NVDAB")
    assert nv["narrative"] == "AI_CHIPS" and "status" in nv and "tradable" in nv and "min_order_usdt" in nv
    full = c.get("/api/tokens", params={"all": 1}).json()
    assert len(full) >= len(rows)
    for r in full:
        assert r["narrative"] in NARRATIVES
        assert {"symbol", "name", "status", "tradable", "min_order_usdt"} <= set(r)

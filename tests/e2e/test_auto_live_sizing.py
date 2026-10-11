"""Hotfix: LIVE AUTO sizes from the real wallet, not the Auto Mode capital (no network, never spawns baw).

Rishu's PC: AUTO picked GRID on a ~110 USDT wallet and proposed 25 USDT buys (2% of a large Auto Mode capital hit the
25 USDT cap). Through the real app, engine and risk, with a fake LIVE wallet of 109.87 USDT and AutoConfig.capital=10000,
the GRID BUY must be 10 USDT (the NVDAB token minimum), not 25.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from backend.contracts import Candle, Timeframe

from .test_small_wallet_live import WEEKDAY, LiveSim, SimClient, _client  # noqa: F401

pytestmark = pytest.mark.skipif(SimClient is None, reason="real engine / risk / SIM client not importable")


def _wiggle_candles(n: int, anchor) -> list[Candle]:
    """Tight sideways range (+-0.2%) ending on a bar in the bottom grid cell: the same shape as Bot 2's unit test."""
    out, price = [], 100.0
    for i in range(n):
        c = price * (1 + 0.002 * (1 if i % 2 else -1))
        o, h, l = price, max(price, c) * 1.001, min(price, c) * 0.999
        out.append((o, h, l, c))
    out.append((99.8, 99.8, 99.74, 99.75))
    t0 = anchor - timedelta(hours=len(out) - 1)
    return [Candle(timestamp=t0 + timedelta(hours=i), open=o, high=h, low=l, close=c, volume=1000.0)
            for i, (o, h, l, c) in enumerate(out)]


def test_auto_grid_on_110_usdt_wallet_proposes_token_minimum_not_25(monkeypatch):
    c, ex = _client(109.87, WEEKDAY, monkeypatch)
    candles = _wiggle_candles(240, ex._anchor)
    ex.get_candles = lambda symbol, timeframe=Timeframe.H1, limit=200: candles[-limit:]
    ex._price_override["NVDAB"] = 99.75
    assert ex.get_token_status("NVDAB").min_order_usdt == 10.0
    assert c.get("/api/wallet").json()  # wallet readable
    # Rishu's PC: the token context reached the engine without a wallet `equity` hint, so live_size's basis was the
    # Auto Mode capital. Drop that hint here so the engine's own basis (min(capital, portfolio equity)) is tested.
    eng = c.app.state.st.loop.c.engine
    eng = getattr(eng, "_primary", None) or eng
    orig = eng.set_market_context
    monkeypatch.setattr(eng, "set_market_context",
                        lambda sym, ctx: orig(sym, {k: v for k, v in (ctx or {}).items() if k != "equity"} if ctx else ctx))

    r = c.post("/api/auto/activate", json={"symbol": "NVDAB", "style": "BALANCED", "capital": 10_000,
                                           "activation": "AUTO_EXECUTE"})
    assert r.status_code == 200, r.text
    ds = [d for d in c.post("/api/loop/tick").json() if d["symbol"] == "NVDAB"]
    buys = [d for d in ds if d["action"] == "BUY" or (d.get("signal") or {}).get("action") == "BUY"]
    assert buys, [d["reason"] for d in ds]
    d = buys[0]
    assert d["strategy"] == "GRID" or "grid" in d["reason"].lower(), d["reason"]
    sig = d.get("signal") or {}
    if sig.get("proposed_notional") is not None:
        assert sig["proposed_notional"] == pytest.approx(10.0), d["reason"]
    assert d["action"] == "BUY" and (d.get("execution") or {}).get("status") == "FILLED", (d["reason"], d.get("execution"))
    n = ex.order_notional[d["execution"]["client_order_id"]]
    assert n == pytest.approx(10.0, abs=0.01), (n, d["reason"])
    assert n < 25.0

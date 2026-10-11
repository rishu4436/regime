"""Stop-loss QA (Bot 6): a price that keeps falling after entry must trigger 0.5% / 2% / 5% stop-losses through the
live loop (SIM loop tick -> risk.check_exits -> SELL) and through the loop's check_exits path alone, at the same bar,
reason and price as Engine.backtest_manual.

Uses Bot 2's `falling_after_entry()` (backend/engine/tests/helpers.py): entry at 101 on bar 60, then 10 bars that each
close 1% lower (low 0.2% under the close), so every stop tighter than ~9.6% must fire. Runs for the app fallbacks and
the real Bot 2 engine + Bot 3 risk.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import ExitReason, Timeframe
from backend.engine.tests.helpers import ENTRY_BAR, ENTRY_PRICE, falling_after_entry, sma_entry_strategy
from backend.mocks import MockExchangeClient

from .helpers import engine_risk_impls
from .test_exit_parity import live_exit

SYMBOL = "AAPLB"  # BStock: no market-hours gate in SIM
SL = [0.5, 2.0, 5.0]


class FallingExchange(MockExchangeClient):
    """Mock client that serves the falling-after-entry series for SYMBOL (the SIM loop replays it bar by bar)."""

    def __init__(self, candles):
        super().__init__(seed=1)
        self.series_ = candles

    def get_candles(self, symbol, timeframe=Timeframe.H1, limit=200):
        if symbol == SYMBOL:
            return list(self.series_[-limit:])
        return super().get_candles(symbol, timeframe, limit)


def _strategy(sl: float):
    s = sma_entry_strategy(stop_loss_pct=sl, take_profit_pct=50.0)
    d = s.model_dump()
    d["symbol"] = SYMBOL
    d["activation"] = "AUTO_EXECUTE"
    from backend.contracts import ManualStrategy
    return ManualStrategy.model_validate(d)


def _backtest(engine, strat, candles):
    trades = engine.backtest_manual(strat, candles, 10_000.0).trades
    assert len(trades) == 1, trades
    return trades[0]


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
@pytest.mark.parametrize("sl", SL)
def test_stop_loss_backtest_and_check_exits_parity(engine, risk, sl):
    cs = falling_after_entry(drop_pct_per_bar=1.0, bars=10)
    strat = _strategy(sl)
    t = _backtest(engine, strat, cs)
    idx = {c.timestamp: i for i, c in enumerate(cs)}
    assert idx[t.entry_time] == ENTRY_BAR and t.entry_price == pytest.approx(ENTRY_PRICE)
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(ENTRY_PRICE * (1 - sl / 100))
    got = live_exit(risk, strat, cs, ENTRY_BAR, t.entry_price, t.quantity)
    assert got is not None and (got[0], got[1]) == (idx[t.exit_time], ExitReason.STOP_LOSS)
    assert got[2] == pytest.approx(t.exit_price, rel=1e-9)


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
@pytest.mark.parametrize("sl", SL)
def test_stop_loss_fires_through_the_live_loop(engine, risk, sl):
    cs = falling_after_entry(drop_pct_per_bar=1.0, bars=10)
    strat = _strategy(sl)
    bt = _backtest(engine, strat, cs)
    s = Settings(sim_seed=1, sim_replay_bars=len(cs))
    c = TestClient(create_app(s, build_components(s, exchange=FallingExchange(cs), engine=engine, risk=risk)))
    r = c.post("/api/manual/activate", json={"strategy": strat.model_dump(mode="json")})
    assert r.status_code == 200, r.text
    ds = []
    for _ in range(len(cs)):
        ds += c.post("/api/loop/tick").json()
    buys = [d for d in ds if d["action"] == "BUY" and (d.get("execution") or {}).get("status") == "FILLED"]
    sells = [d for d in ds if d["action"] == "SELL" and (d.get("execution") or {}).get("status") == "FILLED"]
    assert len(buys) == 1, [d["reason"] for d in ds][:5]
    assert not [d for d in ds if d["action"] == "BUY" and (d.get("execution") or {}).get("status") != "FILLED"]
    assert buys[0]["timestamp"].startswith(cs[ENTRY_BAR].timestamp.isoformat()[:19])
    assert len(sells) == 1, [(d["action"], d["reason"]) for d in ds]
    sell = sells[0]
    assert sell.get("exit_reason") == "STOP_LOSS" or "stop" in sell["reason"].lower(), sell
    assert sell["execution"]["avg_price"] == pytest.approx(bt.exit_price, rel=1e-6)
    assert sell["timestamp"].startswith(bt.exit_time.isoformat()[:19])
    assert c.get("/api/portfolio").json()["positions"] == []

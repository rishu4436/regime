"""SIM must never show future bars: /api/candles, /api/price, /api/tokens and the regime preview stop at the loop's
current replay bar - via the client's set_replay_index(i) when it has one (Bot 4), else by trimming in the app."""
from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import SIM_WARMUP_BARS, Timeframe
from backend.mocks import MockExchangeClient

from .helpers import SEED, trading_strategy


class ReplayMock(MockExchangeClient):
    """Mimics Bot 4's replay API on top of the coordinator mock (fixed n-bar series)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.replay_index: Optional[int] = None
        self.calls: list[Optional[int]] = []

    def set_replay_index(self, i: Optional[int]) -> None:
        self.calls.append(i)
        self.replay_index = i

    def n_bars(self, symbol: str) -> int:
        return self._n

    def get_candles(self, symbol, timeframe=Timeframe.H1, limit=200):
        full = super().get_candles(symbol, timeframe, limit=self._n)
        if self.replay_index is None:
            return full[-limit:]
        return full[: min(self.replay_index, self._n - 1) + 1][-limit:]

    def get_price(self, symbol):
        if self.replay_index is None or symbol in self._price_override:
            return super().get_price(symbol)
        return self.get_candles(symbol, limit=1)[-1].close


def _real_sim_client():
    try:
        from backend.exchange.sim import SimulatedBinanceClient
    except ImportError:
        return None
    return SimulatedBinanceClient if hasattr(SimulatedBinanceClient, "set_replay_index") else None


CLIENTS = [
    pytest.param(lambda n: MockExchangeClient(seed=SEED, n_candles=n), id="mock-no-replay-api"),
    pytest.param(lambda n: ReplayMock(seed=SEED, n_candles=n), id="duck-typed-replay"),
    pytest.param(lambda n: _real_sim_client()(seed=SEED, n_candles=n), id="bot4-sim",
                 marks=pytest.mark.skipif(_real_sim_client() is None, reason="SimulatedBinanceClient.set_replay_index not on this branch")),
]


@pytest.mark.parametrize("make", CLIENTS)
def test_candles_and_prices_never_pass_the_loop_bar(make):
    s = Settings(sim_seed=SEED, sim_replay_bars=300)
    ex = make(s.sim_replay_bars)
    c = TestClient(create_app(s, build_components(s, exchange=ex)))
    strat = trading_strategy(activation="AUTO_EXECUTE")
    c.post("/api/manual/activate", json={"strategy": strat.model_dump(mode="json")})

    def check(now_iso: str) -> None:
        for sym in ("AAPLB", "NVDAon"):
            cs = c.get(f"/api/candles/{sym}?limit=1000").json()
            assert cs and cs[-1]["timestamp"] == now_iso, sym  # ends exactly at the loop's bar, never later
            assert c.get(f"/api/price/{sym}").json()["price"] == pytest.approx(cs[-1]["close"])
            tok = next(t for t in c.get(f"/api/tokens?q={sym}").json() if t["symbol"] == sym)
            assert tok["price"] == pytest.approx(cs[-1]["close"])
        assert c.get("/api/regime/AAPLB").json()["timestamp"] <= now_iso

    # before the first tick: end of the warm-up (the bar the replay sits on)
    first = c.get("/api/candles/AAPLB?limit=1000").json()
    assert len(first) == SIM_WARMUP_BARS
    check(first[-1]["timestamp"])
    for _ in range(40):
        c.post("/api/loop/tick")
        now = c.get("/api/status").json()["timestamp"]
        check(now.replace("+00:00", "Z") if now.endswith("+00:00") else now)
    if isinstance(ex, ReplayMock) or hasattr(ex, "set_replay_index"):
        idx = getattr(ex, "replay_index")
        assert idx == SIM_WARMUP_BARS - 1 + 40  # the loop pinned the client to its current bar
    if isinstance(ex, ReplayMock):
        assert ex.calls[-1] == idx and None not in ex.calls  # history is cached up front, never fetched by un-pinning


def _n_bars(ex, s):
    return ex.n_bars("AAPLB") if hasattr(ex, "n_bars") else s.sim_replay_bars


@pytest.mark.parametrize("make", CLIENTS)
def test_backtest_and_history_do_not_move_with_the_loop(make):
    s = Settings(sim_seed=SEED, sim_replay_bars=300)
    ex = make(s.sim_replay_bars)
    c = TestClient(create_app(s, build_components(s, exchange=ex)))
    strat = trading_strategy(activation="AUTO_EXECUTE")
    req = {"strategy": strat.model_dump(mode="json"), "initial_capital": 10000, "limit": 250}
    bt0 = c.post("/api/backtest", json=req).json()
    hist0 = c.get("/api/candles/AAPLB?history=true").json()
    other0 = c.get("/api/candles/NVDAon?history=true&limit=100").json()
    assert len(hist0) == _n_bars(ex, s) and len(other0) == 100 and other0 == c.get("/api/candles/NVDAon?history=true").json()[-100:]
    assert bt0["equity_curve"][-1]["timestamp"] == hist0[-1]["timestamp"]  # backtest runs to the end of history
    c.post("/api/manual/activate", json={"strategy": strat.model_dump(mode="json")})
    for _ in range(25):
        c.post("/api/loop/tick")
    assert c.post("/api/backtest", json=req).json() == bt0
    assert c.get("/api/candles/AAPLB?history=true").json() == hist0
    assert c.get("/api/candles/NVDAon?history=true&limit=100").json() == other0
    pinned = c.get("/api/candles/AAPLB").json()  # default unchanged: replay-pinned, 200 bars
    now = c.get("/api/status").json()["timestamp"].replace("+00:00", "Z")
    assert len(pinned) == min(200, 60 + 25) and pinned[-1]["timestamp"] == now and pinned[-1]["timestamp"] < hist0[-1]["timestamp"]
    assert pinned == [x for x in hist0 if x["timestamp"] <= now][-200:]

"""Shared e2e helpers: build a fully wired app on a seeded MockExchangeClient, and impl discovery for parity tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from backend.app.fallbacks import FallbackRiskManager, FallbackStrategyEngine
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import (
    Action, AgentMode, Candle, ControlState, ExitRules, ManualStrategy, Portfolio, Regime, Signal, Timeframe,
)
from backend.mocks import MockExchangeClient

SEED = 1234


def make_client(regimes: Optional[dict[str, Regime]] = None, seed: int = SEED, **settings: Any) -> TestClient:
    s = Settings(sim_seed=seed, **settings)
    comps = build_components(s, exchange=MockExchangeClient(regimes=regimes, seed=seed, n_candles=s.sim_replay_bars))
    return TestClient(create_app(s, comps))


def tick_until(client: TestClient, pred, max_ticks: int = 440) -> tuple[Optional[dict], int]:
    for i in range(max_ticks):
        for d in client.post("/api/loop/tick").json():
            if pred(d):
                return d, i
    return None, max_ticks


def run_all(client: TestClient, max_ticks: int = 600) -> list[dict]:
    out: list[dict] = []
    for _ in range(max_ticks):
        ds = client.post("/api/loop/tick").json()
        if not ds and client.get("/api/loop").json()["replay_finished"]:
            break
        out.extend(ds)
    return out


def trading_strategy(symbol: str = "AAPLB", activation: str = "CONFIRMATION") -> ManualStrategy:
    """A manual rule set that trades regularly on the seeded mocks (RSI<45 OR near lower Bollinger band)."""
    s = ManualStrategy(name="e2e", symbol=symbol)
    d = s.model_dump()
    d["entry"]["logic"] = "OR"
    d["entry"]["rsi_below"].update(enabled=True, threshold=45)
    d["entry"]["price_above_sma"]["enabled"] = False
    d["entry"]["bollinger_lower"]["enabled"] = True
    d["exit"].update(take_profit_pct=3.0, stop_loss_pct=2.0)
    d["exit"]["rsi_above"]["threshold"] = 60
    d["risk"]["cooldown_bars"] = 2
    d["activation"] = activation
    return ManualStrategy.model_validate(d)


# ------------------------------------------------------------------ real-vs-fallback implementation discovery
def _probe_engine(e) -> bool:
    try:
        e.backtest_manual(ManualStrategy(), _flat(70), 1000.0)
        return True
    except NotImplementedError:
        return False


def _probe_risk(r) -> bool:
    try:
        r.check_exits(Portfolio(starting_capital=1000, cash=1000, equity=1000), {})
        r.check(Signal(symbol="X", action=Action.STAY_OUT, source=AgentMode.MANUAL, strategy="p", reason="p"),
                Portfolio(starting_capital=1000, cash=1000, equity=1000), ControlState())
        return True
    except NotImplementedError:
        return False


def _flat(n: int) -> list[Candle]:
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [Candle(timestamp=t0 + timedelta(hours=i), open=100, high=100, low=100, close=100, volume=1000) for i in range(n)]


def engine_risk_impls():
    """pytest params: the app's fallbacks (always) and the real Bot 2 / Bot 3 classes (skipped until implemented)."""
    from backend.engine.engine import DefaultStrategyEngine
    from backend.risk.manager import DefaultRiskManager

    real_e, real_r = DefaultStrategyEngine(), DefaultRiskManager()
    ready = _probe_engine(real_e) and _probe_risk(real_r)
    return [
        pytest.param(FallbackStrategyEngine(), FallbackRiskManager(), id="fallback"),
        pytest.param(real_e, real_r, id="real",
                     marks=pytest.mark.skipif(not ready, reason="real engine/risk not implemented on this branch yet")),
    ]

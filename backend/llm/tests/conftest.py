from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from backend.contracts import (
    Action, AgentMode, ControlState, Decision, ExecutionPath, ExecutionResult, OrderSide, OrderStatus, Portfolio,
    Position, PresetName, Regime, RiskCheck, RiskVerdict, Signal, StatusSnapshot, TradingMode,
)
from backend.llm.client import GrokClient, GrokConfig


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    """Tests must never hit the real API, even if the developer has XAI_API_KEY exported."""
    for k in ("XAI_API_KEY", "XAI_MODEL", "XAI_BASE_URL", "XAI_TIMEOUT_S", "LLM_DISABLED"):
        monkeypatch.delenv(k, raising=False)


def chat_response(content: str) -> dict:
    return {"id": "x", "object": "chat.completion", "model": "grok-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def make_client():
    """make_client(handler) -> (GrokClient with a mocked transport, Recorder)."""
    def _make(handler, key: str | None = "test-key", **cfg):
        rec = Recorder(handler)
        http = httpx.Client(transport=httpx.MockTransport(rec))
        return GrokClient(GrokConfig(api_key=key, model=cfg.pop("model", "grok-test"), **cfg), http=http), rec
    return _make


@pytest.fixture
def reply():
    """reply("text") -> handler returning a successful chat completion with that content."""
    return lambda content: (lambda req: httpx.Response(200, json=chat_response(content)))


@pytest.fixture
def buy_decision() -> Decision:
    sig = Signal(symbol="NVDAon", action=Action.BUY, source=AgentMode.AUTO, strategy="TREND_FOLLOWING",
                 preset=PresetName.TREND_FOLLOWING, regime=Regime.STRONG_UPTREND, reason="price above rising EMA20 with momentum",
                 conditions_met=["price_above_ema", "macd_bullish_cross"], conditions_failed=["volume_above_avg"],
                 price=142.5, proposed_size_pct=10, proposed_notional=1000)
    v = RiskVerdict(signal_id=sig.id, approved=True, adjusted_size_pct=10, adjusted_notional=1000,
                    checks=[RiskCheck(name="max_exposure", passed=True)])
    ex = ExecutionResult(client_order_id="c1", symbol="NVDAon", side=OrderSide.BUY, status=OrderStatus.FILLED,
                         path=ExecutionPath.SWAP, mode=TradingMode.SIM, filled_quantity=7.0, avg_price=142.5, notional=997.5, fee=1.0)
    return Decision(mode=TradingMode.SIM, agent_mode=AgentMode.AUTO, symbol="NVDAon", regime=Regime.STRONG_UPTREND,
                    strategy="TREND_FOLLOWING", action=Action.BUY, reason="trend following entry", signal=sig, risk_verdict=v, execution=ex)


@pytest.fixture
def blocked_decision() -> Decision:
    sig = Signal(symbol="TSLAx", action=Action.BUY, source=AgentMode.MANUAL, strategy="MANUAL:dip", regime=Regime.HIGH_VOLATILITY,
                 reason="RSI below 35", conditions_met=["rsi_below"])
    v = RiskVerdict(signal_id=sig.id, approved=False, reasons=["cooldown active (2 of 5 bars)"],
                    checks=[RiskCheck(name="cooldown", passed=False, detail="2/5")])
    return Decision(mode=TradingMode.SIM, agent_mode=AgentMode.MANUAL, symbol="TSLAx", regime=Regime.HIGH_VOLATILITY,
                    strategy="MANUAL:dip", action=Action.STAY_OUT, reason="blocked by risk", signal=sig, risk_verdict=v)


@pytest.fixture
def status(buy_decision) -> StatusSnapshot:
    pos = Position(symbol="NVDAon", quantity=7.0, avg_entry_price=142.5, strategy="TREND_FOLLOWING", current_price=150.0, unrealized_pnl=52.5)
    pf = Portfolio(starting_capital=10000, cash=9002.5, positions=[pos], realized_pnl=120.0, equity=10052.5, trades_today=2, daily_pnl=40.0)
    return StatusSnapshot(control=ControlState(), portfolio=pf, agent_mode=AgentMode.AUTO, active_symbol="NVDAon",
                          current_regime=Regime.STRONG_UPTREND, active_strategy="TREND_FOLLOWING", last_decision=buy_decision,
                          recent_decisions=[buy_decision])


@pytest.fixture
def empty_status() -> StatusSnapshot:
    return StatusSnapshot(control=ControlState(paused=True, reason="user pause"),
                          portfolio=Portfolio(starting_capital=10000, cash=10000, equity=10000))

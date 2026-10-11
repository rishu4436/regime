"""The loop must execute on the path the quote was issued for (Quote.path): RFQ -> execute_rfq, SWAP -> execute_swap.
Bot 4's clients REJECT an order sent to the wrong path; this mock does the same."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import ExecutionPath, ExecutionResult, ManualStrategy, OrderStatus
from backend.mocks import MockExchangeClient

from .helpers import SEED, tick_until, trading_strategy

WEEKDAY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # Wed 15:00 UTC: every SIM token is TRADING


class PathStrictMock(MockExchangeClient):
    def __init__(self, path: ExecutionPath, **kw) -> None:
        super().__init__(**kw)
        self.path = path
        self.calls: list[str] = []

    def get_quote(self, symbol, side, notional, path=None):
        return super().get_quote(symbol, side, notional, path=self.path)

    def _strict(self, name, expected, order, quote):
        self.calls.append(name)
        if quote.path != expected:
            return ExecutionResult(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side,
                                   status=OrderStatus.REJECTED, path=expected, mode=self.mode, error="wrong execution path")
        return getattr(super(), name)(order, quote)

    def execute_swap(self, order, quote):
        return self._strict("execute_swap", ExecutionPath.SWAP, order, quote)

    def execute_rfq(self, order, quote):
        return self._strict("execute_rfq", ExecutionPath.RFQ, order, quote)


@pytest.mark.parametrize("path", [ExecutionPath.RFQ, ExecutionPath.SWAP])
def test_loop_dispatches_on_quote_path(path):
    s = Settings(sim_seed=SEED)
    ex = PathStrictMock(path, seed=SEED, n_candles=s.sim_replay_bars)
    c = TestClient(create_app(s, build_components(s, exchange=ex)))
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    buy, _ = tick_until(c, lambda d: d["action"] == "BUY")
    assert buy is not None and buy["execution"]["status"] == "FILLED" and buy["execution"]["path"] == path.value
    sell, _ = tick_until(c, lambda d: d["action"] == "SELL")
    assert sell is not None and sell["execution"]["path"] == path.value
    want = "execute_rfq" if path == ExecutionPath.RFQ else "execute_swap"
    assert ex.calls and set(ex.calls) == {want}
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]


@pytest.mark.parametrize("symbol", ["NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"])
def test_every_token_executes_on_whatever_path_it_was_quoted(symbol):
    """No per-token path assumption: SIM routing is the exchange's business (Bot 4 #5: Ondo + xStock -> RFQ,
    bStock -> SWAP; the plain mock quotes SWAP). Whatever the installed SIM client quotes, the fill must be on it."""
    s = Settings(sim_seed=SEED)
    comps = build_components(s)
    if callable(getattr(comps.exchange, "set_trading_status", None)):  # Bot 4 SIM client: Ondo is off-hours (no fills,
        try:                                                             # 40367) on Sat/Sun UTC -> pin a weekday clock
            comps = build_components(s, exchange=type(comps.exchange)(seed=SEED, n_candles=s.sim_replay_bars, clock=lambda: WEEKDAY))
        except TypeError:
            pass
    ex = comps.exchange
    quoted: list[ExecutionPath] = []
    orig = ex.get_quote

    def spy(*a, **kw):
        q = orig(*a, **kw)
        quoted.append(q.path)
        return q

    ex.get_quote = spy  # instance attribute: works for the mock and for Bot 4's client
    c = TestClient(create_app(s, comps))
    d = trading_strategy(symbol, activation="AUTO_EXECUTE").model_dump()
    d["entry"]["rsi_below"]["threshold"] = 100  # enter on (almost) any bar: this test is about routing, not signals
    strat = ManualStrategy.model_validate(d)
    c.post("/api/manual/activate", json={"strategy": strat.model_dump(mode="json")})
    fills = []
    for _ in range(440):
        fills += [d["execution"] for d in c.post("/api/loop/tick").json() if d.get("execution")]
        if len(fills) >= 2 or (not quoted and _ > 30):
            break
    if symbol.endswith("x") and not quoted:  # Bot 4 d1aeef4: xStock off by default -> a no-trade decision, not a failure
        ds = [d for d in c.get("/api/decisions?limit=50").json() if d["symbol"] == symbol]
        assert not fills and ds and all(d["action"] == "STAY_OUT" for d in ds)
        assert any("no quote" in d["reason"] and "xStock" in d["reason"] for d in ds)
        assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]
        return
    assert fills, f"no executions for {symbol}"
    assert len(quoted) >= len(fills)
    for e, q in zip(fills, quoted):
        assert e["status"] == "FILLED", e
        assert e["path"] == q.value
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]

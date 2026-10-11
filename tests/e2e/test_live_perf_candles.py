"""LIVE: /api/performance uses the same baseline as /api/portfolio (no fake 99% drawdown on a ~110 USDT wallet), and
exchange candle errors (Binance rejecting limit > ~200-300 bars) are a plain 502, never an uncaught 500.
Fake clients only: no network, no keys, no baw."""
from __future__ import annotations

from backend.exchange.errors import Web3ApiError

from .test_live import SYM, FakeLiveExchange, _buy, buy_strategy, first_buy_tick, live_client

WALLET = {"BNB": 0.05, "USDT": 109.87}


def _perf_ok(c):
    pf = c.get("/api/portfolio").json()
    perf = c.get("/api/performance").json()
    assert perf["starting_capital"] == pf["starting_capital"]
    assert perf["equity_curve"][0]["equity"] == pf["starting_capital"]
    assert perf["max_drawdown_pct"] < 20, perf["max_drawdown_pct"]
    assert abs(perf["total_return_pct"]) < 20
    return pf, perf


def test_live_performance_baseline_is_the_wallet_capital():
    c, ex = live_client(ex=FakeLiveExchange(balances=dict(WALLET)))
    assert c.get("/api/portfolio").json()["starting_capital"] == 109.87
    d = _buy(first_buy_tick(c, ex, sl=30.0))
    assert d["execution"]["status"] == "FILLED"
    for _ in range(3):
        ex.advance()
        c.post("/api/loop/tick")
    _perf_ok(c)


def test_live_restart_with_unreadable_wallet_keeps_saved_baseline(tmp_path):
    """The real case: state saved at 109.87 USDT; on restart the wallet can't be read, so the book is built at the
    10000 placeholder before the saved state is restored. The curve used to start at 10000 -> ~98.9% drawdown."""
    c, ex = live_client(ex=FakeLiveExchange(balances=dict(WALLET)), state_dir=str(tmp_path))
    _buy(first_buy_tick(c, ex, sl=30.0))
    ex2 = FakeLiveExchange(balances=dict(WALLET))
    ex2.cursor = ex.cursor
    ok = ex2.get_balances
    ex2.get_balances = lambda: (_ for _ in ()).throw(RuntimeError("wallet unavailable at boot"))
    c2, _ = live_client(ex=ex2, state_dir=str(tmp_path))
    ex2.get_balances = ok
    pf, perf = _perf_ok(c2)
    assert pf["starting_capital"] == 109.87 and len(pf["positions"]) == 1
    ex2.advance()
    c2.post("/api/loop/tick")
    _perf_ok(c2)
    ans = c2.post("/api/llm/ask", json={"question": "How is PnL?"})
    if ans.status_code == 200:  # explainer reads the same tracker: no 98.9 drawdown repeated
        assert "98.9" not in ans.text


class StrictCandlesEx(FakeLiveExchange):
    """Binance-like: more than 200 bars per request is rejected with a Web3ApiError."""

    def get_candles(self, symbol, timeframe=None, limit=200):
        if limit > 200:
            raise Web3ApiError(50011, "Parameter limit error")
        return super().get_candles(symbol, timeframe, limit) if timeframe else super().get_candles(symbol, limit=limit)


def test_candles_exchange_error_is_a_plain_502_not_500():
    c, _ = live_client(ex=StrictCandlesEx())
    ok = c.get(f"/api/candles/{SYM}", params={"timeframe": "1h", "limit": 200, "history": True})
    assert ok.status_code == 200 and len(ok.json()) == 200
    for params in ({"timeframe": "1h", "limit": 500, "history": True}, {"timeframe": "1h", "limit": 500},
                   {"timeframe": "1h", "history": True}):  # history default is 500 bars
        r = c.get(f"/api/candles/{SYM}", params=params)
        assert r.status_code == 502, (params, r.status_code, r.text)
        msg = r.json()["detail"]
        assert f"Couldn't load {SYM} 1h candles" in msg and "Web3ApiError" in msg


def test_backtest_candle_error_is_a_plain_502():
    c, _ = live_client(ex=StrictCandlesEx())
    r = c.post("/api/backtest", json={"strategy": buy_strategy(), "limit": 500})
    assert r.status_code == 502 and "candles" in r.json()["detail"]
    r = c.post("/api/backtest", json={"strategy": buy_strategy(), "limit": 200})
    assert r.status_code == 200

from datetime import timedelta

from backend.contracts import (
    ExchangeClient, ExecutionPath, OrderRequest, OrderSide, OrderStatus, Regime, TradingMode, utc_now,
)
from backend.mocks import MockExchangeClient, generate_candles


def _sma(xs, n):
    return sum(xs[-n:]) / n


def _rsi(closes, n=14):
    gains = [max(b - a, 0) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    losses = [max(a - b, 0) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    g, l = sum(gains) / n, sum(losses) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def test_deterministic():
    assert generate_candles(Regime.MILD_UPTREND, seed=7) == generate_candles(Regime.MILD_UPTREND, seed=7)
    assert generate_candles(Regime.MILD_UPTREND, seed=7) != generate_candles(Regime.MILD_UPTREND, seed=8)


def test_regime_shapes():
    c = {r: [x.close for x in generate_candles(r, n=250)] for r in Regime}
    assert c[Regime.STRONG_UPTREND][-1] > _sma(c[Regime.STRONG_UPTREND], 50) * 1.05
    assert c[Regime.MILD_UPTREND][-1] > _sma(c[Regime.MILD_UPTREND], 50)
    assert c[Regime.DOWNTREND][-1] < _sma(c[Regime.DOWNTREND], 50)
    side = c[Regime.SIDEWAYS]
    assert abs(side[-1] / side[-100] - 1) < 0.08
    assert _rsi(c[Regime.OVERSOLD_PULLBACK]) < 30
    rets = lambda xs: [abs(b / a - 1) for a, b in zip(xs, xs[1:])]
    assert sum(rets(c[Regime.HIGH_VOLATILITY])) > 3 * sum(rets(side))


def test_mock_exchange_flow():
    now = [utc_now()]
    ex = MockExchangeClient(clock=lambda: now[0])
    assert isinstance(ex, ExchangeClient) and ex.mode == TradingMode.SIM
    assert {t.symbol for t in ex.search_tokens("")} >= {"NVDAon", "TSLAx", "AAPLB"}
    assert [t.symbol for t in ex.search_tokens("tesla")] == ["TSLAx"]
    assert len(ex.get_candles("NVDAon", limit=100)) == 100
    q = ex.get_quote("NVDAon", OrderSide.BUY, 1000)
    res = ex.execute_swap(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=1000, quote_id=q.quote_id), q)
    assert res.status == OrderStatus.FILLED and res.filled_quantity > 0 and res.path == ExecutionPath.SWAP
    q2 = ex.get_quote("NVDAon", OrderSide.BUY, 1000, path=ExecutionPath.RFQ)
    now[0] = now[0] + timedelta(seconds=31)
    res2 = ex.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=1000), q2)
    assert res2.status == OrderStatus.EXPIRED
    q3 = ex.get_quote("NVDAon", OrderSide.BUY, 10)
    live = ex.execute_swap(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=10, mode=TradingMode.LIVE), q3)
    assert live.status == OrderStatus.REJECTED

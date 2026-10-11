import pytest
from backend.contracts import ExecutionPath, OrderRequest, OrderSide, OrderStatus, Timeframe
from backend.exchange.sim import SimulatedBinanceClient
from backend.mocks import MockExchangeClient


@pytest.fixture(autouse=True)
def _xstock_on(monkeypatch):
    """These tests cover xStock routing mechanics; xStock is disabled by default (see test_token_status.py)."""
    monkeypatch.setenv("BINANCE_ENABLE_XSTOCK", "true")


def full(c, sym="NVDAon", tf=Timeframe.H1):
    return c.get_candles(sym, tf, limit=c.n_bars(sym))


def test_default_none_matches_mock():
    c = SimulatedBinanceClient()
    assert c.replay_index is None and c.n_bars("NVDAon") == 250
    assert c.get_candles("NVDAon", limit=200) == MockExchangeClient().get_candles("NVDAon", limit=200)


def test_candles_up_to_index_inclusive_with_limit():
    c = SimulatedBinanceClient()
    series = full(c)
    c.set_replay_index(100)
    assert c.replay_index == 100
    assert c.get_candles("NVDAon", limit=500) == series[:101]
    got = c.get_candles("NVDAon", limit=20)
    assert got == series[81:101] and got[-1] == series[100]
    assert c.get_candles("NVDAon", Timeframe.D1, limit=5)[-1].timestamp > series[100].timestamp  # per-timeframe series


def test_price_quote_and_fill_use_bar_close():
    c = SimulatedBinanceClient()
    series = full(c, "TSLAx")
    c.set_replay_index(50)
    px = series[50].close
    assert c.get_price("TSLAx") == px
    q = c.get_quote("TSLAx", OrderSide.BUY, 100)
    assert q.price == px and q.path == ExecutionPath.RFQ
    r = c.execute_rfq(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=100), q)
    assert r.status == OrderStatus.FILLED and r.avg_price == px
    assert {t.symbol: t.price for t in c.search_tokens("tsla")}["TSLAx"] == px


def test_clamping_and_reset():
    c = SimulatedBinanceClient()
    series = full(c)
    c.set_replay_index(10_000)
    assert c.get_price("NVDAon") == series[-1].close and len(c.get_candles("NVDAon", limit=999)) == 250
    c.set_replay_index(-5)
    assert c.get_candles("NVDAon", limit=10) == series[:1] and c.get_price("NVDAon") == series[0].close
    c.set_replay_index(None)
    assert c.replay_index is None and c.get_price("NVDAon") == MockExchangeClient().get_price("NVDAon")


def test_last_index_equals_default_and_override_wins():
    c = SimulatedBinanceClient()
    default_px = c.get_price("AAPLB")
    c.set_replay_index(249)
    assert c.get_price("AAPLB") == default_px
    c.set_price("AAPLB", 123.0)
    assert c.get_price("AAPLB") == 123.0


def test_regime_change_respected():
    from backend.contracts import Regime

    c = SimulatedBinanceClient()
    c.set_replay_index(120)
    before = c.get_price("SPYon")
    c.set_regime("SPYon", Regime.DOWNTREND)
    assert c.get_price("SPYon") == full(c, "SPYon")[120].close
    assert c.get_price("SPYon") != before

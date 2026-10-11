"""LIVE/PAPER: stop-loss runs on the latest price even when no new candle window arrived (Bot 4, 2026-10-11)."""
from types import SimpleNamespace

from backend.app.loop import TradingLoop


def test_exits_on_price_skips_when_nothing_held():
    loop = TradingLoop.__new__(TradingLoop)
    loop.wallet_block = None
    loop.book = SimpleNamespace(lots_for=lambda s: [])
    assert loop._exits_on_price("NVDAB") is None


def test_exits_on_price_no_price_is_safe():
    loop = TradingLoop.__new__(TradingLoop)
    loop.wallet_block = None
    loop.book = SimpleNamespace(lots_for=lambda s: [object()])
    loop._last_close = {"NVDAB": 100.0}
    def boom(sym):
        raise RuntimeError("network")
    loop.c = SimpleNamespace(exchange=SimpleNamespace(get_price=boom))
    assert loop._exits_on_price("NVDAB") is None


def test_wallet_block_means_no_exit_orders():
    loop = TradingLoop.__new__(TradingLoop)
    loop.wallet_block = {"address": "0x1", "expected": "0x2"}
    assert loop._exits_on_price("NVDAB") is None


def test_live_stop_loss_fires_without_new_candle(monkeypatch):
    import pytest
    from . import test_small_wallet_live as sw
    if sw.SimClient is None:
        pytest.skip("real engine/risk not importable")
    c, ex = sw._client(500.0, sw.WEEKDAY, monkeypatch)
    ds = sw._run(c, ex, "NVDAB", ticks=3)
    assert any(d["action"] == "BUY" for d in ds), [d.get("reason") for d in ds]
    c.post("/api/loop/tick")  # consume the window opened by the last advance()
    assert c.post("/api/loop/tick").json() == []  # no new candle: nothing to do yet
    held = c.app.state.st.loop.book.lots_for("NVDAB")
    assert held
    entry = held[0].position.avg_entry_price
    ex._price_override["NVDAB"] = entry * 0.5  # crash, but NO new candle window (no ex.advance())
    first = c.post("/api/loop/tick").json()  # >3% from the last close: deferred, needs a second reading
    assert not [d for d in first if d["action"] == "SELL"]
    out = c.post("/api/loop/tick").json()  # same price again: confirmed, the stop-loss sells
    sells = [d for d in out if d["symbol"] == "NVDAB" and d["action"] == "SELL"]
    assert sells, [d.get("reason") for d in out]


def test_single_bad_print_does_not_sell(monkeypatch):
    import pytest
    from . import test_small_wallet_live as sw
    if sw.SimClient is None:
        pytest.skip("real engine/risk not importable")
    c, ex = sw._client(500.0, sw.WEEKDAY, monkeypatch)
    ds = sw._run(c, ex, "NVDAB", ticks=3)
    assert any(d["action"] == "BUY" for d in ds)
    c.post("/api/loop/tick")
    entry = c.app.state.st.loop.book.lots_for("NVDAB")[0].position.avg_entry_price
    ex._price_override["NVDAB"] = entry * 0.5  # one bad print
    assert not [d for d in c.post("/api/loop/tick").json() if d["action"] == "SELL"]
    ex._price_override.pop("NVDAB")  # back to normal on the next reading
    assert not [d for d in c.post("/api/loop/tick").json() if d["action"] == "SELL"]
    assert c.app.state.st.loop.book.lots_for("NVDAB")

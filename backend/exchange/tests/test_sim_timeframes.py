"""SIM replay is pinned to a time, shared by every timeframe (Bot 1's timeframe-switch bug, Bot 6's QA)."""
from datetime import datetime, timedelta, timezone

import pytest
from backend.contracts import OrderRequest, OrderSide, OrderStatus, Timeframe
from backend.exchange.sim import SimulatedBinanceClient
from backend.exchange.status import TradingStatus
from backend.mocks.candles import TIMEFRAME_DELTA

WED = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)


def test_series_are_time_aligned_and_share_one_price_path():
    c = SimulatedBinanceClient()
    h1 = c.get_full_candles("NVDAon", Timeframe.H1)
    for tf in Timeframe:
        s = c.get_full_candles("NVDAon", tf)
        assert len(s) == c.n_bars("NVDAon", tf)
        step = TIMEFRAME_DELTA[tf]
        assert all(b.timestamp - a.timestamp == step for a, b in zip(s, s[1:]))
        assert all(b.open == a.close for a, b in zip(s, s[1:]))  # continuous
        assert all(x.low <= min(x.open, x.close) and x.high >= max(x.open, x.close) for x in s)
        by_ts = {x.timestamp: x.close for x in s}
        shared = [b for b in h1 if b.timestamp in by_ts]
        assert shared and all(by_ts[b.timestamp] == b.close for b in shared)  # same close at the same time
        assert s[-1].timestamp <= h1[-1].timestamp
    assert c.get_full_candles("NVDAon", Timeframe.M1)[0].timestamp == h1[0].timestamp
    assert c.get_full_candles("NVDAon", Timeframe.M1)[-1].timestamp == h1[-1].timestamp
    assert c.get_full_candles("NVDAon", Timeframe.D1)[0].timestamp < h1[0].timestamp  # history for 1d warm-up


def test_switching_timeframe_keeps_the_clock_and_price():
    c = SimulatedBinanceClient()
    c.set_replay_index(130)  # a 1h loop at bar 130
    t = c.replay_time
    px = c.get_price("NVDAon")
    assert t == c.get_full_candles("NVDAon")[130].timestamp
    for tf in Timeframe:
        last = c.get_candles("NVDAon", tf, limit=500)[-1]
        assert last.timestamp <= t and t - last.timestamp < TIMEFRAME_DELTA[tf]
        i = c.index_at("NVDAon", tf, t)
        c.set_replay_index(i, tf)  # the loop resumes on another timeframe at the same time
        assert c.replay_time == last.timestamp and c.replay_time <= t
        assert c.get_price("NVDAon") == last.close
        c.set_replay_time(t)
    assert c.get_price("NVDAon") == px
    # a 1m loop moves forward minute by minute from there, never back to the start of the data
    i = c.index_at("NVDAon", Timeframe.M1, t)
    c.set_replay_index(i + 1, Timeframe.M1)
    assert c.replay_time == t + timedelta(minutes=1)
    assert c.get_quote("NVDAon", OrderSide.BUY, 25).price == c.get_candles("NVDAon", Timeframe.M1, 1)[-1].close


def test_status_price_follows_the_pin():
    c = SimulatedBinanceClient(clock=lambda: WED)
    c.set_replay_index(60)
    st = c.get_token_status("NVDAon")
    assert st.price == c.get_full_candles("NVDAon")[60].close == c.get_price("NVDAon")


def test_ondo_off_hours_quotes_but_does_not_fill():
    c = SimulatedBinanceClient(clock=lambda: SUN)
    assert c.get_token_status("NVDAon").trading_status == TradingStatus.OFFHOURS
    for side in (OrderSide.BUY, OrderSide.SELL):
        q = c.get_quote("NVDAon", side, 25)
        r = c.execute_rfq(OrderRequest(symbol="NVDAon", side=side, notional=25), q)
        assert r.status == OrderStatus.REJECTED and r.error.startswith("40367")
    q = c.get_quote("NVDAB", OrderSide.BUY, 25)  # bStock trades 24/7
    assert c.execute_swap(OrderRequest(symbol="NVDAB", side=OrderSide.BUY, notional=25), q).status == OrderStatus.FILLED


@pytest.mark.parametrize("status", [TradingStatus.CLOSED, TradingStatus.OFFHOURS])
def test_forced_closed_status_rejects(status):
    c = SimulatedBinanceClient(clock=lambda: WED)
    q = c.get_quote("NVDAon", OrderSide.BUY, 25)
    assert c.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=25), q).status == OrderStatus.FILLED
    c.set_trading_status("NVDAon", status)
    q = c.get_quote("NVDAon", OrderSide.BUY, 25)
    assert c.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=25), q).status == OrderStatus.REJECTED

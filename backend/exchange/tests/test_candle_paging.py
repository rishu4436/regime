"""Paging of GET /market/candles in BinanceWeb3Client.get_candles.

Real API behaviour (NVDAB 1h/15m, probed 2026-10-11 from an India IP):
- limit 100/200/299/300 OK, limit 301/500 -> code 40001 "invalid limit range" (HTTP 200)
- `after=<ts>` returns the newest bars with timestamp <= ts (inclusive, older history)
- `before=<ts>` returns bars newer than ts
"""
from __future__ import annotations

import pytest

from backend.contracts import Timeframe, TradingMode
from backend.exchange.errors import Web3ApiError
from backend.exchange.live import CANDLE_PAGE_SIZE, BinanceWeb3Client
from backend.exchange.tests._fakes import err, ok

H = 3_600_000
END = 1_791_694_800_000  # newest 1h NVDAB bar seen live


def row(ts: int) -> list:
    px = 100 + (ts // H) % 7
    return [px, px + 1, px - 1, px + 0.5, 10, ts, 3]


class FakeCandleApi:
    """Mimics /market/candles: `cap` max limit, inclusive `after`, ascending rows."""

    def __init__(self, n_bars: int, cap: int = 300, extra_overlap: int = 0, empty_on_call: int | None = None,
                 fail_on_call: int | None = None) -> None:
        self.bars = [END - i * H for i in range(n_bars)][::-1]  # ascending
        self.cap, self.extra_overlap = cap, extra_overlap
        self.empty_on_call, self.fail_on_call = empty_on_call, fail_on_call
        self.calls: list[dict] = []

    def get_candles(self, chain_id, address, bar="1h", limit=100, after=None, before=None):
        self.calls.append({"limit": limit, "after": after, "before": before, "bar": bar})
        n = len(self.calls)
        if limit > self.cap:
            raise Web3ApiError(40001, "invalid limit range", 200)
        if self.fail_on_call == n:
            raise Web3ApiError(50000, "boom", 500)
        if self.empty_on_call == n:
            return []
        pool = [t for t in self.bars if after is None or t <= after]
        page = pool[-limit:]
        if after is not None and self.extra_overlap:  # misbehaving server: also repeats newer bars
            newer = [t for t in self.bars if t > after][: self.extra_overlap]
            page = page + newer
        return [row(t) for t in page]


def client(api, **kw) -> BinanceWeb3Client:
    c = BinanceWeb3Client(api, mode=TradingMode.PAPER, **kw)
    c.sleeps = []
    c._candle_sleep = c.sleeps.append
    return c


def ts_list(candles):
    return [int(k.timestamp.timestamp() * 1000) for k in candles]


def test_page_size_matches_real_cap():
    assert CANDLE_PAGE_SIZE == 300


@pytest.mark.parametrize("limit", [1, 200, 300])
def test_single_request_when_within_page(limit):
    api = FakeCandleApi(1000)
    out = client(api).get_candles("NVDAB", Timeframe.H1, limit=limit)
    assert len(api.calls) == 1 and api.calls[0]["limit"] == limit and api.calls[0]["after"] is None
    assert ts_list(out) == api.bars[-limit:]


@pytest.mark.parametrize("cap", [200, 300])
def test_pages_under_cap(cap):
    api = FakeCandleApi(2000, cap=cap)
    c = client(api)
    c.candle_page_size = cap
    out = c.get_candles("NVDAB", Timeframe.H1, limit=500)
    assert ts_list(out) == api.bars[-500:]  # newest 500, ascending, contiguous, no dupes
    assert all(call["limit"] <= cap for call in api.calls)
    assert api.calls[0]["after"] is None
    for prev, call in zip(api.calls, api.calls[1:]):
        assert call["after"] is not None and call["before"] is None
    # page 2 resumes at the oldest bar of page 1 (inclusive -> one overlapping bar)
    assert api.calls[1]["after"] == api.bars[-cap]
    assert c.sleeps and all(s >= 0.2 for s in c.sleeps)  # <= 5 RPS per endpoint
    assert len(c.sleeps) == len(api.calls) - 1


def test_old_cap_would_have_failed_without_paging():
    api = FakeCandleApi(1000)
    with pytest.raises(Web3ApiError):
        api.get_candles("56", "0x", limit=500)


def test_short_final_page_stops():
    api = FakeCandleApi(420)  # less history than asked for
    out = client(api).get_candles("NVDAB", Timeframe.H1, limit=1000)
    assert ts_list(out) == api.bars  # everything, once
    assert len(api.calls) == 2  # 300 + short page of 121 (incl. overlap) -> stop


def test_overlapping_duplicates_are_deduped():
    api = FakeCandleApi(2000, extra_overlap=5)
    out = client(api).get_candles("NVDAB", Timeframe.H1, limit=700)
    ts = ts_list(out)
    assert len(ts) == len(set(ts)) == 700 and ts == sorted(ts) and ts == api.bars[-700:]


def test_empty_page_stops_and_returns_what_we_have():
    api = FakeCandleApi(2000, empty_on_call=2)
    out = client(api).get_candles("NVDAB", Timeframe.H1, limit=900)
    assert ts_list(out) == api.bars[-300:] and len(api.calls) == 2


def test_empty_first_page():
    api = FakeCandleApi(0)
    assert client(api).get_candles("NVDAB", Timeframe.H1, limit=500) == [] and len(api.calls) == 1


def test_later_page_error_returns_partial_but_first_page_error_raises():
    api = FakeCandleApi(2000, fail_on_call=2)
    assert ts_list(client(api).get_candles("NVDAB", Timeframe.H1, limit=900)) == api.bars[-300:]
    with pytest.raises(Web3ApiError):
        client(FakeCandleApi(2000, fail_on_call=1)).get_candles("NVDAB", Timeframe.H1, limit=900)


def test_page_cap_bounds_requests():
    api = FakeCandleApi(100_000)
    c = client(api)
    out = c.get_candles("NVDAB", Timeframe.H1, limit=50_000)
    assert len(api.calls) == c.candle_max_pages and len(out) == len(set(ts_list(out)))


def test_zero_limit_makes_no_request():
    api = FakeCandleApi(10)
    assert client(api).get_candles("NVDAB", Timeframe.H1, limit=0) == [] and api.calls == []


def test_paging_over_http_uses_after_param(fake, api):
    """Wire-level: the signed client sends limit<=300 and `after` on the second page; 15m works too."""
    bars = [END - i * 900_000 for i in range(600)][::-1]

    def handler(req):
        p = fake.params(req)
        lim = int(p["limit"])
        if lim > 300:
            return err(40001, "invalid limit range", 200)
        after = int(p["after"]) if "after" in p else None
        assert "before" not in p
        pool = [t for t in bars if after is None or t <= after]
        return ok([row(t) for t in pool[-lim:]])

    fake.on("GET", "/api/v1/dex/market/candles", handler)
    c = client(api)
    out = c.get_candles("NVDAB", Timeframe.M15, limit=500)
    assert ts_list(out) == bars[-500:]
    ps = [fake.params(r) for r in fake.requests]
    assert [p["limit"] for p in ps] == ["300", "201"] and "after" not in ps[0] and ps[1]["after"] == str(bars[-300])
    assert all(p["bar"] == "15m" for p in ps)

"""Token status / min order / multiplier / price impact / non-exception quote reasons.
Fixtures in samples/ are real public Binance responses fetched Sun 2026-10-11 ~01:50 UTC (Bot 2)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from backend.contracts import Candle, OrderRequest, OrderSide, OrderStatus, TradingMode
from backend.exchange.errors import QuoteUnavailable
from backend.exchange.live import BinanceWeb3Client
from backend.exchange.sim import SimulatedBinanceClient
from backend.exchange.status import (
    QuoteReason,
    TradingStatus,
    parse_rwa_dynamic,
    price_impact_pct,
    reason_for_error_code,
    status_from_candles,
    status_from_status_info,
    xstock_enabled,
)
from backend.exchange.tests._fakes import Clock, err, ok
from backend.exchange.tokens import DEFAULT_TOKENS
from backend.exchange.web3_api import PublicRwaClient

S = Path(__file__).parent / "samples"
NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)  # Sunday, when the samples were taken
WEEKDAY = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
WALLET = "0x000000000000000000000000000000000000bEEF"
NVDA_ON = next(t for t in DEFAULT_TOKENS if t.symbol == "NVDAon").contract_address


def load(name):
    return json.loads((S / f"{name}.json").read_text())


def kline_candles(name):  # public kline/ai rows: [openTime, o, h, l, c, volume, closeTime]
    return [Candle(timestamp=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc), open=float(r[1]),
                   high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in load(name)["data"]["klineInfos"]]


@pytest.fixture(autouse=True)
def _xstock_default(monkeypatch):
    monkeypatch.delenv("BINANCE_ENABLE_XSTOCK", raising=False)


# ---------------------------------------------------------------- parsers on real samples
def test_status_info_from_real_samples():
    assert status_from_status_info(load("status_NVDAon")["data"]) == TradingStatus.OFFHOURS
    assert status_from_status_info(load("rwa_dynamic_NVDAon")["data"]["statusInfo"]) == TradingStatus.OFFHOURS
    assert status_from_status_info(load("rwa_dynamic_NVDAB")["data"]["statusInfo"]) == TradingStatus.TRADING
    # documented codes (binance-tokenized-securities-info SKILL.md)
    assert status_from_status_info({"openState": False, "marketStatus": "closed", "reasonCode": "MARKET_CLOSED"}) == TradingStatus.CLOSED
    assert status_from_status_info({"openState": True, "reasonCode": "ASSET_PAUSED", "reasonMsg": "stock_split"}) == TradingStatus.CLOSED
    assert status_from_status_info({"openState": True, "marketStatus": "regular", "reasonCode": "ASSET_LIMITED"}) == TradingStatus.TRADING
    assert status_from_status_info(None) == TradingStatus.UNKNOWN


def test_rwa_dynamic_multiplier_and_reference_price():
    on = parse_rwa_dynamic(load("rwa_dynamic_NVDAon")["data"])
    assert on["shares_multiplier"] == pytest.approx(1.0017152488) and on["reference_price"] == 230.475
    assert on["max_notional_usdt"] == 1_000_000
    b = parse_rwa_dynamic(load("rwa_dynamic_NVDAB")["data"])  # stockInfo.price null -> price / multiplier
    assert b["reference_price"] == pytest.approx(230.4792249 / 1.0007782238)


def test_status_from_real_candles():
    assert status_from_candles(kline_candles("kline_NVDAB_1h"), NOW) == TradingStatus.TRADING
    assert status_from_candles(kline_candles("kline_NVDAon_1h"), NOW) == TradingStatus.OFFHOURS  # zero volume
    assert status_from_candles(kline_candles("kline_TSLAx_1h"), NOW) == TradingStatus.NO_LIQUIDITY  # last bar 4 Oct
    assert status_from_candles([], NOW) == TradingStatus.UNKNOWN


def test_error_codes_and_impact_and_env():
    assert reason_for_error_code(40367) == "market_closed" and reason_for_error_code(40369) == "market_closed"
    assert reason_for_error_code(40374) == "insufficient_liquidity" and reason_for_error_code(40375) == "below_minimum"
    assert reason_for_error_code(40101) is None
    assert price_impact_pct({"priceImpactPercent": "0.0001"}, OrderSide.BUY, 1, 1) == 0.0001
    assert price_impact_pct({}, OrderSide.BUY, 101, 100) == pytest.approx(1.0)
    assert price_impact_pct(None, OrderSide.SELL, 99, 100) == pytest.approx(1.0)
    assert price_impact_pct(None, OrderSide.BUY, 101, None) is None
    assert not xstock_enabled({}) and xstock_enabled({"BINANCE_ENABLE_XSTOCK": "true"})


# ---------------------------------------------------------------- SIM
def test_sim_statuses_weekend():
    c = SimulatedBinanceClient(clock=Clock(NOW))
    b, on, x = c.get_token_status("AAPLB"), c.get_token_status("NVDAon"), c.get_token_status("TSLAx")
    assert (b.trading_status, b.tradable, b.min_order_usdt) == (TradingStatus.TRADING, True, 10.0)
    assert (on.trading_status, on.tradable, on.min_order_usdt) == (TradingStatus.OFFHOURS, True, 20.0)
    assert on.shares_multiplier == pytest.approx(1.0017152488) and on.reference_price < on.price
    assert (x.trading_status, x.tradable, x.reason) == (TradingStatus.NO_LIQUIDITY, False, "xstock_disabled")
    assert c.get_token_status("NVDAon").to_dict()["trading_status"] == "OFFHOURS"


def test_sim_weekday_ondo_trading_and_overrides():
    c = SimulatedBinanceClient(clock=Clock(WEEKDAY))
    assert c.get_token_status("NVDAon").trading_status == TradingStatus.TRADING
    c.set_trading_status("AAPLB", TradingStatus.CLOSED)
    r = c.quote_or_reason("AAPLB", OrderSide.BUY, 25)
    assert not r.ok and r.reason == "market_closed" and r.quote is None
    assert SimulatedBinanceClient(clock=Clock(WEEKDAY), enable_xstock=True).get_token_status("TSLAx").tradable


def test_sim_quote_or_reason():
    c = SimulatedBinanceClient(clock=Clock(NOW))
    r = c.quote_or_reason("NVDAon", OrderSide.BUY, 15)
    assert not r.ok and r.reason == QuoteReason.BELOW_MINIMUM.value and r.min_order_usdt == 20
    r = c.quote_or_reason("AAPLB", OrderSide.BUY, 5)
    assert r.reason == "below_minimum"
    assert c.quote_or_reason("NVDAon", OrderSide.SELL, 5).ok  # exits are never blocked by the minimum
    r = c.quote_or_reason("TSLAx", OrderSide.BUY, 50)
    assert not r.ok and r.reason == "xstock_disabled" and r.to_dict()["reason"] == "xstock_disabled"
    with pytest.raises(QuoteUnavailable) as e:  # contract get_quote still raises, with a reason
        c.get_quote("TSLAx", OrderSide.BUY, 50)
    assert e.value.reason == "xstock_disabled"
    r = c.quote_or_reason("NVDAon", OrderSide.BUY, 25)
    assert r.ok and r.trading_status == TradingStatus.OFFHOURS and r.price_impact_pct == 0.0
    assert c.quote_meta(r.quote.quote_id) is r
    assert c.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=25), r.quote).status == OrderStatus.FILLED


# ---------------------------------------------------------------- PAPER (mocked HTTP, real public payloads)
def public_client(handler):
    return PublicRwaClient(http=httpx.Client(transport=httpx.MockTransport(handler)))


def dyn_handler(sample):
    def h(req):
        assert req.url.path.endswith("/rwa/dynamic/ai") and req.url.params["chainId"] == "56"
        assert req.headers["User-Agent"].startswith("binance-web3/")
        return httpx.Response(200, json=load(sample))
    return h


def paper(api, clock, **kw):
    return BinanceWeb3Client(api, mode=TradingMode.PAPER, wallet_address=WALLET, clock=clock, **kw)


def route(to_amount, impact="0.0001"):
    return {"quoteId": "bq1", "vendorName": "LiquidMesh", "executionMode": "SWAP", "toTokenAmount": str(to_amount),
            "tradeFee": "0.0119", "isBest": True, "priceImpactPercent": impact}


def test_paper_status_from_public_rwa_dynamic(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([{"tokenContractAddress": NVDA_ON, "tokenPrice": "230.87",
                                                        "referencePrice": "230.475"}]))
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route(int(0.108403 * 10**18))]))
    c = paper(api, clock, public=public_client(dyn_handler("rwa_dynamic_NVDAon")))
    st = c.get_token_status("NVDAon")
    assert st.trading_status == TradingStatus.OFFHOURS and st.tradable and st.source == "rwa_dynamic"
    assert st.min_order_usdt == 20 and st.max_order_usdt == 1_000_000 and st.market_status == "offhours"
    assert st.shares_multiplier == pytest.approx(1.0017152488) and st.reference_price == 230.475
    assert st.next_open_time == datetime(2026, 10, 12, 0, 5, tzinfo=timezone.utc)
    r = c.quote_or_reason("NVDAon", OrderSide.BUY, 25)
    assert r.ok and r.price_impact_pct == 0.0001 and r.trading_status == TradingStatus.OFFHOURS
    assert c.quote_meta(r.quote.quote_id).price_impact_pct == 0.0001
    assert sum("/candles" in str(q.url) for q in fake.requests) == 0  # statusInfo present -> no candle fallback


@pytest.mark.parametrize("code,reason", [(40367, "market_closed"), (40369, "market_closed"),
                                         (40374, "insufficient_liquidity"), (40375, "below_minimum")])
def test_paper_api_refusals_become_reasons(fake, api, clock, code, reason):
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([]))
    fake.on("GET", "/api/v1/dex/market/candles", ok([]))
    fake.on("GET", "/api/v1/dex/aggregator/quote", err(code, "refused"))
    r = paper(api, clock).quote_or_reason("NVDAon", OrderSide.BUY, 25)
    assert not r.ok and r.reason == reason and r.error_code == code and r.quote is None


def test_paper_auth_errors_still_raise(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([]))
    fake.on("GET", "/api/v1/dex/market/candles", ok([]))
    fake.on("GET", "/api/v1/dex/aggregator/quote", err(40101, "API key invalid", 401))
    with pytest.raises(QuoteUnavailable):
        paper(api, clock).quote_or_reason("NVDAon", OrderSide.BUY, 25)


def test_paper_candle_fallback_and_closed_status(fake, api):
    clock = Clock(NOW)
    rows = [[r[1], r[2], r[3], r[4], r[5], r[0]] for r in load("kline_NVDAon_1h")["data"]["klineInfos"]]
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([]))
    fake.on("GET", "/api/v1/dex/market/candles", ok(rows))
    fail_public = public_client(lambda req: httpx.Response(503, text="down"))
    st = paper(api, clock, public=fail_public).get_token_status("NVDAon")
    assert st.trading_status == TradingStatus.OFFHOURS and st.source == "candles"
    paused = {"code": "000000", "success": True, "data": {"tokenInfo": {"price": "230", "sharesMultiplier": "1"},
              "statusInfo": {"openState": True, "marketStatus": "regular", "reasonCode": "ASSET_PAUSED",
                             "reasonMsg": "stock_split"}}}
    c = paper(api, clock, public=public_client(lambda req: httpx.Response(200, json=paused)))
    r = c.quote_or_reason("NVDAon", OrderSide.BUY, 25)
    assert not r.ok and r.reason == "market_closed" and "stock_split" in r.message
    assert not any("/aggregator/quote" in str(q.url) for q in fake.requests)  # refused before spending a quote


def test_paper_xstock_disabled_by_default_no_network(fake, api, clock):
    c = paper(api, clock)
    st = c.get_token_status("TSLAx")
    assert (st.trading_status, st.tradable, st.reason) == (TradingStatus.NO_LIQUIDITY, False, "xstock_disabled")
    assert c.quote_or_reason("TSLAx", OrderSide.BUY, 25).reason == "xstock_disabled"
    assert fake.requests == []

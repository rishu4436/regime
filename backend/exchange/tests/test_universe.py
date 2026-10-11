"""Full BSC token universe (list_tokens) and the runtime LIVE per-order cap (set_live_limits)."""
import json
import threading
from pathlib import Path

import pytest

from backend.contracts import OrderRequest, OrderSide, OrderStatus, Quote, Timeframe, TokenType, TradingMode
from backend.exchange.baw import BawCli
from backend.exchange.baw_executor import BawExecutor, IntentStore
from backend.exchange.executors import RouteContext
from backend.exchange.live import BinanceWeb3Client
from backend.exchange.sim import SimulatedBinanceClient
from backend.exchange.tests._fakes import err, ok
from backend.exchange.tokens import DEFAULT_TOKENS
from backend.exchange.universe import (
    apply_price_info,
    count_by_type,
    merge_rows,
    rows_from_public_list,
    rows_from_rwa_tokens,
)

SAMPLES = Path(__file__).parent / "samples"
RWA_TOKENS = json.loads((SAMPLES / "rwa_tokens_bsc.json").read_text())["data"]
PUBLIC_LIST = json.loads((SAMPLES / "rwa_public_list_bsc.json").read_text())["data"]
PRICE_INFO = json.loads((SAMPLES / "price_info_bsc.json").read_text())["data"]
ROW_KEYS = {"symbol", "name", "type", "contract_address", "decimals", "underlying", "price", "volume_24h", "liquidity",
            "min_order_usdt", "max_order_usdt", "status", "tradable"}
STATUSES = {"TRADING", "OFFHOURS", "CLOSED", "NO_LIQUIDITY", "UNKNOWN"}
GATE_ON = {"LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_ENABLED": "true", "RISK_ALLOW_LIVE": "true"}


class FakePublic:
    def __init__(self, rows=PUBLIC_LIST, fail=False):
        self.rows, self.fail, self.calls = rows, fail, 0

    def rwa_token_list(self, list_type=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("public down")
        return self.rows

    def rwa_dynamic(self, *a):
        raise RuntimeError("not in this test")


def price_info_handler(request):
    body = json.loads(request.content)
    by = {r["tokenContractAddress"]: r for r in PRICE_INFO}
    return ok([by[b["tokenContractAddress"]] for b in body if b["tokenContractAddress"] in by])


def paper(api, clock, public=None, **kw):
    c = BinanceWeb3Client(api, mode=TradingMode.PAPER, clock=clock, public=public, **kw)
    c._candle_sleep = lambda s: None
    return c


# ------------------------------------------------------------------ parsing (real 2026-10-11 responses)
def test_parse_real_samples_counts_and_rows():
    api_rows = rows_from_rwa_tokens(RWA_TOKENS)
    assert count_by_type(api_rows) == {"bStock": 46, "Ondo": 442}
    apply_price_info(api_rows, PRICE_INFO)
    rows = merge_rows(api_rows, rows_from_public_list(PUBLIC_LIST))
    assert count_by_type(rows) == {"bStock": 91, "Ondo": 458, "xStock": 130}
    by = {r["symbol"]: r for r in rows}
    for r in rows:
        assert ROW_KEYS <= set(r) and r["status"] in STATUSES and isinstance(r["tradable"], bool)
    n = by["NVDAB"]
    assert (n["name"], n["type"], n["underlying"], n["decimals"]) == ("NVIDIA (bStocks)", "bStock", "NVDA", 18)
    assert n["contract_address"] == "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
    assert n["status"] == "TRADING" and n["tradable"] is True and n["min_order_usdt"] == 10.0
    assert n["price"] == pytest.approx(230.659, rel=1e-4)
    assert n["volume_24h"] == pytest.approx(378131.88)        # DEX buy + sell, not the 17bn US-stock volume
    assert n["underlying_volume_24h"] == pytest.approx(17054711459)
    o = by["NVDAon"]
    assert o["type"] == "Ondo" and o["status"] == "OFFHOURS" and o["min_order_usdt"] == 20.0
    assert by["AAPLon"]["status"] == "OFFHOURS" and by["AAPLon"]["reason_code"] == "TRADING"
    closed = [r for r in rows if r["status"] == "CLOSED"]
    assert closed and all(not r["tradable"] and r["reason"] == "market_closed" for r in closed)
    x = by["TSLAx"]   # xStock: still disabled / no liquidity
    assert x["type"] == "xStock" and x["status"] == "NO_LIQUIDITY" and not x["tradable"] and x["reason"] == "xstock_disabled"
    a = by["AAPLB"]   # in baw's public list, not in the Web3 API list
    assert a["status"] == "UNKNOWN" and a["tradable"] is False and a["reason"] == "unlisted"
    assert by["QQQB"]["liquidity"] == pytest.approx(9.985, rel=1e-3)
    assert "narrative" not in n and "sector" not in n       # the API has no sector (tabId is ignored)


def test_xstock_enabled_rows_are_tradable_unknown():
    rows = rows_from_public_list(PUBLIC_LIST, enable_xstock=True)
    x = {r["symbol"]: r for r in rows}["TSLAx"]
    assert x["status"] == "UNKNOWN" and x["tradable"] is True


# ------------------------------------------------------------------ PAPER/LIVE client
def test_list_tokens_full_universe_cached_10_min(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS))
    fake.on("POST", "/api/v1/dex/market/price-info", price_info_handler)
    pub = FakePublic()
    c = paper(api, clock, public=pub)
    rows = c.list_tokens()
    assert count_by_type(rows) == {"bStock": 91, "Ondo": 458, "xStock": 130}
    assert c.universe_source == "rwa_tokens" and c.universe_error is None
    tok_req = [r for r in fake.requests if r.url.path.endswith("/rwa/tokens")]
    assert fake.params(tok_req[0]) == {"binanceChainId": "56"}
    pi = [r for r in fake.requests if r.url.path.endswith("/price-info")]
    assert len(pi) == 5 and all(len(json.loads(r.content)) <= 100 for r in pi)   # 488 rows -> 5 batches
    n_req = len(fake.requests)
    clock.advance(9 * 60)
    assert len(c.list_tokens()) == len(rows) and len(fake.requests) == n_req and pub.calls == 1
    clock.advance(2 * 60)
    c.list_tokens()
    assert len(fake.requests) > n_req and pub.calls == 2


def test_token_resolves_any_listed_symbol(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS))
    fake.on("POST", "/api/v1/dex/market/price-info", ok([]))
    fake.on("GET", "/api/v1/dex/market/rwa/search", ok([]))
    fake.on("GET", "/api/v1/dex/market/candles", ok([[1, 1, 1, 1, 5, 1791699200000, 1]]))
    c = paper(api, clock, public=FakePublic())
    t = c._token("MSFTB")   # not in the registry, search finds nothing -> universe
    assert t.contract_address == "0x80106cb3ead06659a5ad19df39d9b4733863b9b0" and t.token_type == TokenType.BSTOCK
    assert c.get_candles("MSFTB", Timeframe.H1, limit=1)[0].close == 1
    assert c._token("METAon").underlying == "META"    # cached universe, no extra search
    with pytest.raises(Exception):
        c._token("NOPE")


def test_list_tokens_falls_back_to_public_then_registry_then_previous(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", err(40304, "region blocked", 403))
    c = paper(api, clock, public=FakePublic())
    rows = c.list_tokens()
    assert c.universe_source == "public_list" and "rwa/tokens" in (c.universe_error or "")
    assert count_by_type(rows)["bStock"] == 91
    assert {t.symbol for t in DEFAULT_TOKENS} <= {r["symbol"] for r in rows}
    nvdab = {r["symbol"]: r for r in rows}["NVDAB"]
    assert nvdab["status"] == "UNKNOWN" and nvdab["tradable"] is True   # API list down: unknown, not "unlisted"

    c2 = paper(api, clock, public=FakePublic(fail=True))
    rows2 = c2.list_tokens()
    assert c2.universe_source == "registry" and {r["symbol"] for r in rows2} == {t.symbol for t in DEFAULT_TOKENS}

    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS))
    fake.on("POST", "/api/v1/dex/market/price-info", ok([]))
    c3 = paper(api, clock, public=None)
    good = c3.list_tokens()
    assert count_by_type(good) == {"bStock": 47, "Ondo": 442, "xStock": 2}   # + registry AAPLB, TSLAx, METAx
    aaplb = {r["symbol"]: r for r in good}["AAPLB"]
    assert aaplb["reason"] != "unlisted" and aaplb["source"] in ("registry", "registry+token_status")
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", err(50000, "boom", 500))
    clock.advance(601)
    assert len(c3.list_tokens()) == len(good) and "boom" in c3.universe_error


def test_price_info_failure_is_best_effort(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS[:3]))
    fake.on("POST", "/api/v1/dex/market/price-info", err(50000, "boom", 500))
    rows = paper(api, clock).list_tokens()
    first = {r["symbol"]: r for r in rows}[RWA_TOKENS[0]["tokenSymbol"]]
    assert first["price"] is not None and first["volume_24h"] is None


# ------------------------------------------------------------------ SIM
def test_sim_list_tokens_has_real_universe_and_search_unchanged():
    c = SimulatedBinanceClient()
    rows = c.list_tokens()
    by = {r["symbol"]: r for r in rows}
    assert len(rows) >= 30 and count_by_type(rows)["bStock"] >= 15
    assert {"MSFTB", "METAB", "QQQB", "AAPLon", "AMZNon", "NVDAB", "TSLAon", "TSLAx"} <= set(by)
    for r in rows:
        assert ROW_KEYS <= set(r) and r["status"] in STATUSES and r["price"] > 0
    assert by["TSLAx"]["tradable"] is False and by["MSFTB"]["status"] == "TRADING"
    assert by["MSFTB"]["contract_address"] == "0x80106cb3ead06659a5ad19df39d9b4733863b9b0"
    assert by["QQQB"]["volume_24h"] == pytest.approx(3759228.78)
    # the loop/feed list is unchanged; named search and every market call reach the universe
    assert "MSFTB" not in {t.symbol for t in c.search_tokens("")}
    assert [t.symbol for t in c.search_tokens("microsoft")] == ["MSFTB", "MSFTon"]
    assert len(c.get_candles("MSFTB", Timeframe.H1, limit=60)) == 60
    q = c.get_quote("MSFTB", OrderSide.BUY, 25)
    res = c.execute_swap(OrderRequest(symbol="MSFTB", side=OrderSide.BUY, notional=25, mode=TradingMode.SIM), q)
    assert res.status == OrderStatus.FILLED


# ------------------------------------------------------------------ runtime LIVE per-order cap
def make_baw(tmp_path, env=None):
    return BawExecutor(cli=BawCli(runner=lambda *a, **k: None, cmd=["baw"]), intents=IntentStore(tmp_path / "i.json"),
                       env={**GATE_ON, **(env or {})}, lock=threading.Lock())


def test_baw_set_live_limits_changes_buy_cap_at_runtime(tmp_path):
    ex = make_baw(tmp_path)
    assert ex.max_order_usdt == 25 and ex.max_sell_usdt == 50
    lim = ex.set_live_limits(max_order_usdt=200)
    assert lim == {"max_order_usdt": 200.0, "max_sell_usdt": 400.0, "default_max_order_usdt": 25.0,
                   "hard_max_order_usdt": None}
    from backend.contracts import ExecutionPath
    from backend.exchange import baw as B

    def attempt(usdt):
        order = OrderRequest(symbol="NVDAB", side=OrderSide.BUY, notional=usdt, mode=TradingMode.LIVE)
        quote = Quote(symbol="NVDAB", side=OrderSide.BUY, notional=usdt, quantity=1, price=usdt, fee=0)
        ctx = RouteContext(chain_id="56", from_token="u", to_token="t", from_decimals=18, to_decimals=18, amount="1",
                           from_qty=usdt)
        try:
            ex.execute(order, quote, ctx, ExecutionPath.SWAP)
        except Exception:  # noqa: BLE001 - the fake runner can't trade; we only check the cap gate
            pass
        return ex.last_extras.get("reason")

    assert attempt(250) == B.ORDER_CAP_EXCEEDED
    assert attempt(150) != B.ORDER_CAP_EXCEEDED   # passed the cap (fails later on the fake runner)
    client = BinanceWeb3Client(api=None, mode=TradingMode.LIVE, executor=ex, live_enabled=True,  # type: ignore[arg-type]
                               max_live_notional=50)
    lim = client.set_live_limits(max_order_usdt=100)
    assert lim["max_order_usdt"] == 100 and lim["max_sell_usdt"] == 200 and client.max_live_notional == 200
    assert ex.max_order_usdt == 100


def test_set_live_limits_validation(tmp_path):
    ex = make_baw(tmp_path, {"BINANCE_LIVE_HARD_MAX_ORDER_USDT": "500"})
    for bad in (0, -5, "abc", None, float("nan"), float("inf"), True, 501):
        with pytest.raises(ValueError):
            ex.set_live_limits(max_order_usdt=bad)
    assert ex.max_order_usdt == 25           # unchanged after refusals
    assert ex.set_live_limits(500)["max_order_usdt"] == 500
    with pytest.raises(ValueError):
        ex.set_live_limits(100, max_sell_usdt=50)   # exits must fit
    assert ex.set_live_limits(100, max_sell_usdt=150)["max_sell_usdt"] == 150
    ex2 = make_baw(tmp_path, {"BINANCE_LIVE_MAX_SELL_USDT": "1000"})
    assert ex2.set_live_limits(100)["max_sell_usdt"] == 1000   # explicit env exit cap kept when larger


def test_client_set_live_limits_paper_and_sim(api, clock, monkeypatch):
    monkeypatch.delenv("BINANCE_LIVE_HARD_MAX_ORDER_USDT", raising=False)
    c = paper(api, clock)
    assert c.live_limits()["max_order_usdt"] == 25
    lim = c.set_live_limits(max_order_usdt=200)
    assert lim["max_order_usdt"] == 200 and c.max_live_notional == 200
    with pytest.raises(ValueError):
        c.set_live_limits(0)
    s = SimulatedBinanceClient()
    assert s.set_live_limits(150)["max_order_usdt"] == 150
    monkeypatch.setenv("BINANCE_LIVE_HARD_MAX_ORDER_USDT", "100")
    with pytest.raises(ValueError):
        s.set_live_limits(150)


# ------------------------------------------------------------------ follow-ups (live run 2026-10-11 13:28 IST)
AAPLB = "0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a"


class DynPublic(FakePublic):
    """Public client whose rwa/dynamic answers TRADING for AAPLB (like the old /api/tokens token_status path)."""

    def rwa_dynamic(self, chain_id, addr):
        if addr.lower() != AAPLB:
            raise RuntimeError("not in this test")
        return {"tokenInfo": {"price": "254.10", "sharesMultiplier": "1"}, "stockInfo": {"price": "254.0"},
                "statusInfo": {"openState": True, "marketStatus": None, "reasonCode": "TRADING"},
                "limitInfo": {"maxActiveNotionalValue": "5000"}}


def test_registry_token_missing_from_rwa_tokens_uses_token_status(fake, api, clock):
    assert AAPLB not in {r["tokenContractAddress"] for r in RWA_TOKENS}   # real: AAPLB absent from rwa/tokens
    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS))
    fake.on("POST", "/api/v1/dex/market/price-info", ok([]))
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([]))
    c = paper(api, clock, public=DynPublic())
    by = {r["symbol"]: r for r in c.list_tokens()}
    a = by["AAPLB"]
    st = c.get_token_status("AAPLB")
    assert a["status"] == st.trading_status.value == "TRADING" and a["tradable"] is True and a["reason"] is None
    assert a["price"] == pytest.approx(254.10) and a["max_order_usdt"] == 5000 and a["min_order_usdt"] == 10
    assert a["source"] == "registry+token_status" and a["name"] == "Apple BStock"
    # non-registry tokens that are only in the public list keep the "unlisted" behaviour
    crmb = by["CRMB"]
    assert crmb["status"] == "UNKNOWN" and crmb["tradable"] is False and crmb["reason"] == "unlisted"
    # registry tokens that ARE in rwa/tokens still come from rwa/tokens
    assert by["NVDAB"]["source"] == "rwa_tokens"
    # registry xStocks stay disabled
    assert by["TSLAx"]["tradable"] is False and by["TSLAx"]["reason"] == "xstock_disabled"


def test_volume_none_when_missing_from_price_info_zero_when_really_zero():
    rows = rows_from_rwa_tokens(RWA_TOKENS)
    pltrb = "0x0ca5d51d0277bd006fd9607d3e560785ebad8222"
    mrvlb = "0x16cd4fe7e8880ecc3ba222795229e20489fc2c76"
    apply_price_info(rows, [
        {"tokenContractAddress": pltrb, "buyVolume24H": "0", "sellVolume24H": "0"},        # real 0 (PLTRB, live)
        {"tokenContractAddress": "0x02fca66c1d1afb4e2a7884261eb00f63598a7436", "buyVolume24H": None,
         "sellVolume24H": None},                                                            # present but null
    ])
    by = {r["contract_address"]: r for r in rows}
    assert by[pltrb]["volume_24h"] == 0.0
    assert by["0x02fca66c1d1afb4e2a7884261eb00f63598a7436"]["volume_24h"] is None
    assert by[mrvlb]["volume_24h"] is None                                                  # absent -> None
    # the saved real batch: PLTRB is in it with an explicit 0/0
    assert any(r["tokenContractAddress"] == pltrb and r["buyVolume24H"] == "0" and r["sellVolume24H"] == "0"
               for r in PRICE_INFO)


def test_price_info_batches_cover_every_row_and_gaps_stay_none(fake, api, clock):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.extend(b["tokenContractAddress"] for b in body)
        return ok([{"tokenContractAddress": b["tokenContractAddress"], "buyVolume24H": "1", "sellVolume24H": "2"}
                   for b in body[1:]])  # first token of each batch missing from the response

    fake.on("GET", "/api/v1/dex/market/rwa/tokens", ok(RWA_TOKENS))
    fake.on("POST", "/api/v1/dex/market/price-info", handler)
    rows = [r for r in paper(api, clock).list_tokens() if r["source"] == "rwa_tokens"]
    assert len(seen) == len(rows) == 488 and len(set(seen)) == 488
    missing = [r for r in rows if r["volume_24h"] is None]
    assert len(missing) == 5 and all(r["volume_24h"] == 3.0 for r in rows if r not in missing)

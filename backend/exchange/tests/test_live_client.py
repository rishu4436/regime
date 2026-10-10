import json

import pytest

from backend.contracts import ExchangeClient, ExecutionPath, OrderRequest, OrderSide, OrderStatus, Timeframe, TokenType, TradingMode
from backend.exchange.errors import QuoteUnavailable
from backend.exchange.executors import AgenticWalletExecutor, Web3ApiExecutor
from backend.exchange.live import BinanceWeb3Client
from backend.exchange.tests._fakes import err, ok
from backend.exchange.tokens import QUOTE_ASSETS

NVDA = "0xa9ee28c80f960b889dfbd1902055218cba016f75"
TSLA = "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0"
USDT = QUOTE_ASSETS["56"].address
WALLET = "0x000000000000000000000000000000000000bEEF"
E18 = 10 ** 18


def route(mode, to_amount, quote_id="bq1", vendor="LiquidMesh"):
    return {"quoteId": quote_id, "vendorName": vendor, "executionMode": mode, "toTokenAmount": str(to_amount),
            "fromTokenAmount": "0", "tradeFee": "0.05", "isBest": True}


def paper(api, clock, **kw):
    return BinanceWeb3Client(api, mode=TradingMode.PAPER, wallet_address=WALLET, clock=clock, **kw)


def test_candles_parsed_and_sorted(fake, api, clock):
    rows = [[11, 12, 10, 11.5, 100, 1760000003600000, 5], [10, 11, 9, 10.5, 50, 1760000000000000 // 1000, 2],
            ["bad"]]
    rows[0][5] = 1760003600000
    rows[1][5] = 1760000000000
    fake.on("GET", "/api/v1/dex/market/candles", ok(rows))
    c = paper(api, clock)
    assert isinstance(c, ExchangeClient)
    candles = c.get_candles("NVDAon", Timeframe.H1, limit=10)
    assert [k.close for k in candles] == [10.5, 11.5]
    assert candles[0].timestamp.tzinfo is not None
    p = fake.params(fake.requests[-1])
    assert p["bar"] == "1h" and p["tokenContractAddress"] == NVDA and p["binanceChainId"] == "56"


def test_price_and_search(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/price",
            ok([{"tokenContractAddress": NVDA.upper().replace("0X", "0x"), "tokenPrice": "181.25", "referencePrice": "181"}]))
    fake.on("GET", "/api/v1/dex/market/rwa/search", ok([{"ticker": "NVDA", "companyName": "NVIDIA Corp", "assets": [
        {"platformId": "ondo", "binanceChainId": "56", "tokenContractAddress": NVDA, "tokenSymbol": "NVDAon"},
        {"platformId": "bstock", "binanceChainId": "56", "tokenContractAddress": "0x02fc", "tokenSymbol": "NVDAB"}]}]))
    c = paper(api, clock)
    assert c.get_price("NVDAon") == 181.25
    found = c.search_tokens("nvidia")
    assert [(t.symbol, t.token_type) for t in found] == [("NVDAon", TokenType.ONDO), ("NVDAB", TokenType.BSTOCK)]
    assert fake.params(fake.requests[-1]) == {"keyword": "nvidia"}


def test_buy_quote_rfq_and_paper_fill(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("RFQ", int(0.5 * E18), vendor="PcsXRfq")]))
    c = paper(api, clock)
    q = c.get_quote("NVDAon", OrderSide.BUY, 100)
    p = fake.params(fake.requests[-1])
    assert p["fromTokenAddress"] == USDT and p["toTokenAddress"] == NVDA
    assert p["amount"] == str(100 * E18) and p["userWalletAddress"] == WALLET
    assert q.path == ExecutionPath.RFQ and q.quantity == 0.5 and q.price == 200 and q.fee == 0.05
    assert (q.expires_at - q.created_at).total_seconds() == 30
    o = OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=100, mode=TradingMode.PAPER)
    assert c.execute_swap(o, q).status == OrderStatus.REJECTED  # wrong path
    r = c.execute_rfq(o, q)
    assert r.status == OrderStatus.FILLED and r.mode == TradingMode.PAPER and r.filled_quantity == 0.5
    assert len(fake.requests) == 1  # PAPER never calls execution endpoints


def test_sell_quote_amount_in_token_units(fake, api, clock):
    fake.on("GET", "/api/v1/dex/market/rwa/price", ok([{"tokenContractAddress": TSLA, "tokenPrice": "250"}]))
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("SWAP", int(99.5 * E18))]))
    c = paper(api, clock)
    q = c.get_quote("TSLAx", OrderSide.SELL, 100)
    p = fake.params(fake.requests[-1])
    assert p["fromTokenAddress"] == TSLA and p["amount"] == str(int(0.4 * E18))
    assert q.path == ExecutionPath.SWAP and q.quantity == pytest.approx(0.4) and q.notional == pytest.approx(99.5)


def test_quote_errors_become_quote_unavailable(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", err(40375, "Minimum order amount is 20 USD."))
    with pytest.raises(QuoteUnavailable) as e:
        paper(api, clock).get_quote("NVDAon", OrderSide.BUY, 5)
    assert e.value.code == 40375


def test_expired_quote(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("SWAP", E18)]))
    c = paper(api, clock)
    q = c.get_quote("TSLAx", OrderSide.BUY, 100)
    clock.advance(30)
    r = c.execute_swap(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=100), q)
    assert r.status == OrderStatus.EXPIRED


def test_live_requires_opt_in(api):
    with pytest.raises(ValueError):
        BinanceWeb3Client(api, mode=TradingMode.LIVE)
    with pytest.raises(ValueError):
        BinanceWeb3Client(api, mode=TradingMode.LIVE, executor=AgenticWalletExecutor(runner=lambda a: "{}"))


class FakeSigner:
    address = WALLET

    def __init__(self):
        self.txs, self.typed = [], []

    def sign_transaction(self, tx, chain_id):
        self.txs.append((tx, chain_id))
        return "0xsigned" + str(len(self.txs))

    def sign_typed_data(self, typed):
        self.typed.append(typed)
        return "0x" + "ab" * 65


def live(api, clock, executor, **kw):
    return BinanceWeb3Client(api, mode=TradingMode.LIVE, wallet_address=WALLET, executor=executor,
                             live_enabled=True, clock=clock, **kw)


def test_live_swap_flow_web3_api(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("SWAP", int(0.05 * E18))]))
    fake.on("GET", "/api/v1/dex/aggregator/approve-transaction", ok([{"data": "0x095ea7b3", "gasLimit": "60000", "gasPrice": "1"}]))
    fake.on("GET", "/api/v1/dex/aggregator/swap", ok({"executionMode": "SWAP", "tx": {"from": WALLET, "to": "0xrouter", "data": "0x12", "value": "0", "gas": "200000", "gasPrice": "1"}}))
    fake.on("POST", "/api/v1/dex/pre-transaction/broadcast-transaction", lambda r: ok({"txHash": "0xhash" + json.loads(r.content)["signedTransaction"][-1]}))
    fake.on("GET", "/api/v1/dex/post-transaction/transaction-detail-by-txhash", ok([{"txStatus": "success"}]))
    signer = FakeSigner()
    c = live(api, clock, Web3ApiExecutor(api, signer, sleep=lambda s: None))
    q = c.get_quote("TSLAx", OrderSide.BUY, 10)
    r = c.execute_swap(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=10, mode=TradingMode.LIVE, slippage_bps=50), q)
    assert r.status == OrderStatus.FILLED and r.mode == TradingMode.LIVE and r.tx_hash == "0xhash2"
    assert len(signer.txs) == 2  # approval + swap
    swap_req = next(x for x in fake.requests if x.url.path.endswith("/aggregator/swap"))
    sp = fake.params(swap_req)
    assert sp["quoteId"] == "bq1" and sp["slippagePercent"] == "0.5" and sp["userWalletAddress"] == WALLET
    assert sp["amount"] == str(10 * E18)


def test_live_rfq_flow_web3_api(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("RFQ", int(0.05 * E18), vendor="PcsXRfq")]))
    fake.on("GET", "/api/v1/dex/aggregator/approve-transaction", ok([]))
    typed = {"types": {}, "primaryType": "Order", "domain": {}, "message": {}}
    fake.on("GET", "/api/v1/dex/aggregator/swap", ok({"executionMode": "RFQ", "rfq": {
        "orderId": "rfq-1", "vendor": "PcsXRfq", "typedDataToSign": json.dumps(typed), "signingScheme": "eip712"}}))
    fake.on("POST", "/api/v1/dex/aggregator/order/submit", ok({"orderId": "plat-1", "status": "PENDING_VENDOR"}))
    states = iter([{"orderId": "plat-1", "status": "PENDING_ONCHAIN"},
                   {"orderId": "plat-1", "status": "FILLED", "txHash": "0xfill", "toAmount": str(int(0.049 * E18))}])
    fake.on("GET", "/api/v1/dex/aggregator/order/*", lambda r: ok(next(states)))
    signer = FakeSigner()
    c = live(api, clock, Web3ApiExecutor(api, signer, sleep=lambda s: None))
    q = c.get_quote("NVDAon", OrderSide.BUY, 10)
    o = OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=10, mode=TradingMode.LIVE)
    r = c.execute_rfq(o, q)
    assert r.status == OrderStatus.FILLED and r.tx_hash == "0xfill" and r.filled_quantity == pytest.approx(0.049)
    assert signer.typed == [typed] and signer.txs == []
    sub = next(x for x in fake.requests if x.url.path.endswith("/order/submit"))
    body = fake.body(sub)
    assert body["quoteId"] == "rfq-1" and body["vendor"] == "PcsXRfq" and body["userSignature"].startswith("0x")


def test_live_rfq_expired_quote_from_api(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("RFQ", E18, vendor="PcsXRfq")]))
    fake.on("GET", "/api/v1/dex/aggregator/approve-transaction", ok([]))
    fake.on("GET", "/api/v1/dex/aggregator/swap", err(40401, "QUOTE_EXPIRED"))
    c = live(api, clock, Web3ApiExecutor(api, FakeSigner(), sleep=lambda s: None))
    q = c.get_quote("NVDAon", OrderSide.BUY, 20)
    r = c.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=20, mode=TradingMode.LIVE), q)
    assert r.status == OrderStatus.EXPIRED


def test_live_guards(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("SWAP", E18)]))
    c = live(api, clock, Web3ApiExecutor(api, FakeSigner(), sleep=lambda s: None), max_live_notional=25)
    big = c.get_quote("TSLAx", OrderSide.BUY, 1000)
    r = c.execute_swap(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=1000, mode=TradingMode.LIVE), big)
    assert r.status == OrderStatus.REJECTED and "MAX_NOTIONAL" in r.error
    q = c.get_quote("TSLAx", OrderSide.BUY, 10)
    r = c.execute_swap(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=10, mode=TradingMode.SIM), q)
    assert r.status == OrderStatus.REJECTED and "mode == LIVE" in r.error


def test_live_via_agentic_wallet_cli(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("RFQ", int(0.05 * E18), vendor="PcsXRfq")]))
    calls = []
    listing = iter([{"list": [{"orderId": "77", "status": "PENDING"}]},
                    {"list": [{"orderId": "77", "status": "FINISHED", "txHash": "0xaw"}]}])

    def runner(args):
        calls.append(list(args))
        if args[1:3] == ["wallet", "status"]:
            return json.dumps({"success": True, "data": {"status": "CONNECTED"}})
        if args[1:3] == ["market-order", "swap"]:
            return json.dumps({"success": True, "data": {"orderId": "77"}})
        return json.dumps({"success": True, "data": next(listing)})

    c = live(api, clock, AgenticWalletExecutor(runner=runner, sleep=lambda s: None))
    q = c.get_quote("NVDAon", OrderSide.BUY, 10)
    r = c.execute_rfq(OrderRequest(symbol="NVDAon", side=OrderSide.BUY, notional=10, mode=TradingMode.LIVE, slippage_bps=100), q)
    assert r.status == OrderStatus.FILLED and r.tx_hash == "0xaw" and r.order_id == "77"
    swap = calls[1]
    assert swap[:3] == ["baw", "market-order", "swap"] and swap[-1] == "--json"
    assert swap[swap.index("--fromToken") + 1] == USDT and swap[swap.index("--toToken") + 1] == NVDA
    assert swap[swap.index("--fromTokenQty") + 1] == "10" and swap[swap.index("--slippage") + 1] == "1"


def test_agentic_wallet_not_connected(fake, api, clock):
    fake.on("GET", "/api/v1/dex/aggregator/quote", ok([route("SWAP", E18)]))
    runner = lambda args: json.dumps({"success": True, "data": {"status": "UNCONNECTED"}})
    c = live(api, clock, AgenticWalletExecutor(runner=runner, sleep=lambda s: None))
    q = c.get_quote("TSLAx", OrderSide.BUY, 10)
    r = c.execute_swap(OrderRequest(symbol="TSLAx", side=OrderSide.BUY, notional=10, mode=TradingMode.LIVE), q)
    assert r.status == OrderStatus.REJECTED and "baw auth signin" in r.error

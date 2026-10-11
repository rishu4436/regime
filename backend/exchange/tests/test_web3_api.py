import httpx
import pytest

from backend.exchange.errors import RATE_LIMITED, Web3ApiError
from backend.exchange.signing import sign

from backend.exchange.tests._fakes import err, ok


def test_get_is_signed_over_build_path_and_raw_query(fake, api):
    fake.on("GET", "/api/v1/dex/market/candles", ok([[1, 2, 0.5, 1.5, 10, 1700000000000, 3]]))
    rows = api.get_candles("56", "0xabc", bar="1h", limit=2)
    assert rows == [[1, 2, 0.5, 1.5, 10, 1700000000000, 3]]
    req = fake.requests[-1]
    assert str(req.url).startswith("https://web3.binance.com/build/api/v1/dex/market/candles?")
    raw_path = req.url.raw_path.decode()
    assert req.headers["X-OC-SIGN"] == sign("test-secret", "2026-10-12T15:00:00.000Z", "GET", raw_path)
    assert req.headers["X-OC-APIKEY"] == "test-key-1234"
    assert fake.params(req) == {"binanceChainId": "56", "tokenContractAddress": "0xabc", "bar": "1h", "limit": "2"}


def test_post_signs_compact_json_body(fake, api):
    fake.on("POST", "/api/v1/dex/aggregator/order/submit", ok({"orderId": "o1", "status": "PENDING_VENDOR"}))
    api.submit_rfq_order("req", "0xsig", "PcsXRfq", "q1")
    req = fake.requests[-1]
    body = req.content.decode()
    assert body == '{"requestId":"req","userSignature":"0xsig","vendor":"PcsXRfq","quoteId":"q1"}'
    assert req.headers["X-OC-SIGN"] == sign("test-secret", "2026-10-12T15:00:00.000Z", "POST",
                                            "/build/api/v1/dex/aggregator/order/submit", body)


def test_error_envelope_raises_with_code(fake, api):
    fake.on("GET", "/api/v1/dex/aggregator/quote", err(40367, "ONDO_MARKET_STATE_NOT_TRADABLE"))
    with pytest.raises(Web3ApiError) as e:
        api.get_quote("56", "0xa", "0xb", "1")
    assert e.value.code == 40367


def test_rate_limit_retries_then_succeeds(fake, api):
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"code": 42900, "msg": "rate"})
        return ok([])

    fake.on("GET", "/api/v1/dex/market/rwa/search", handler)
    assert api.search_rwa("nvda") == []
    assert calls["n"] == 2


def test_rate_limit_gives_up(fake, api):
    fake.on("GET", "/api/v1/dex/market/rwa/search",
            httpx.Response(429, headers={"Retry-After": "0"}, json={"code": 42900, "msg": "rate"}))
    with pytest.raises(Web3ApiError) as e:
        api.search_rwa("x")
    assert e.value.code == RATE_LIMITED


def test_repr_hides_secret(api):
    assert "test-secret" not in repr(api)

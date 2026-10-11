import base64
import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from backend.exchange.signing import encode_query, iso_timestamp, sign, signed_headers


def test_iso_timestamp_has_millis_and_z():
    t = datetime(2026, 5, 11, 10, 8, 57, 715432, tzinfo=timezone.utc)
    assert iso_timestamp(t) == "2026-05-11T10:08:57.715Z"


def test_sign_matches_documented_prehash():
    # Example from https://web3.binance.com/en/dev-docs/authentication (Step 3.1, GET request)
    ts, path = "2026-05-11T10:08:57.715Z", "/build/api/v1/dex/market/price?chainId=1&symbol=ETH%20USDT"
    pre = "2026-05-11T10:08:57.715ZGET/build/api/v1/dex/market/price?chainId=1&symbol=ETH%20USDT"
    expected = base64.b64encode(hmac.new(b"secret", pre.encode(), hashlib.sha256).digest()).decode()
    assert sign("secret", ts, "get", path) == expected


def test_post_body_is_part_of_signature():
    a = sign("s", "t", "POST", "/build/api/v1/x", '{"a":1}')
    b = sign("s", "t", "POST", "/build/api/v1/x", '{"a":2}')
    assert a != b


def test_encode_query_uses_percent20_and_drops_none():
    assert encode_query({"symbol": "ETH USDT", "x": None, "flag": True}) == "symbol=ETH%20USDT&flag=true"


def test_signed_headers_requires_build_prefix():
    with pytest.raises(ValueError):
        signed_headers("k", "s", "GET", "/api/v1/dex/market/price")
    h = signed_headers("k", "s", "GET", "/build/api/v1/dex/market/price", timestamp="T", recv_window_ms=10000)
    assert h["X-OC-APIKEY"] == "k" and h["X-OC-TIMESTAMP"] == "T" and h["X-OC-RECV-WINDOW"] == "10000"
    assert h["X-OC-SIGN"] == sign("s", "T", "GET", "/build/api/v1/dex/market/price")

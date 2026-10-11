"""Thin, signed HTTP client for the Binance Web3 API (https://web3.binance.com/build).

Docs: https://web3.binance.com/en/dev-docs/llms.txt (index) / llms-full.txt (full text).
Parameter names mirror the official connector
https://github.com/binance/binance-web3-connector-python (clients/web3-wallet/.../rest_api/api/*.py).

Every method returns the `data` field of the OCResult envelope `{code, msg, data, timestamp, success}`
and raises `Web3ApiError` when `code != 0` or the HTTP status is not 2xx.
Rate limits (authentication#rate-limits): 1200 req/60s per IP and per key, 6000/60s per user, 5 RPS per endpoint;
HTTP 429 carries `Retry-After` (seconds). We retry 429 a bounded number of times.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import httpx

from .errors import RATE_LIMITED, Web3ApiError
from .signing import encode_query, iso_timestamp, signed_headers

DEFAULT_BASE_URL = "https://web3.binance.com/build"
USER_AGENT = "regime-trading-agent/0.1 (+backend/exchange)"


class Web3ApiClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        http: Optional[httpx.Client] = None,
        timeout_s: float = 10.0,
        recv_window_ms: Optional[int] = None,
        max_retries: int = 2,
        max_retry_wait_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        timestamp_fn: Callable[[], str] = iso_timestamp,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self._api_key = api_key
        self._secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._prefix = urlparse(self._base_url).path  # "/build"
        self._http = http or httpx.Client(timeout=timeout_s, headers={"User-Agent": USER_AGENT})
        self._recv_window = recv_window_ms
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait_s
        self._sleep = sleep
        self._ts = timestamp_fn

    def __repr__(self) -> str:  # never leak the secret
        return f"Web3ApiClient(base_url={self._base_url!r}, api_key=***{self._api_key[-4:]})"

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, params: Optional[dict[str, Any]] = None, body: Any = None) -> Any:
        method = method.upper()
        qs = encode_query(params)
        path_qs = f"{path}?{qs}" if qs else path
        raw_body = "" if body is None else json.dumps(body, separators=(",", ":"))
        attempt = 0
        while True:
            headers = signed_headers(
                self._api_key, self._secret, method, self._prefix + path_qs, raw_body,
                timestamp=self._ts(), recv_window_ms=self._recv_window,
            )
            if raw_body:
                headers["Content-Type"] = "application/json"
            resp = self._http.request(method, self._base_url + path_qs, headers=headers, content=raw_body or None)
            if resp.status_code == 429 and attempt < self._max_retries:
                attempt += 1
                try:
                    wait = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                self._sleep(min(max(wait, 0.0), self._max_retry_wait))
                continue
            return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            code = RATE_LIMITED if resp.status_code == 429 else None
            raise Web3ApiError(code, f"HTTP {resp.status_code}: non-JSON response", resp.status_code)
        code = payload.get("code")
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if resp.status_code >= 400 or (code_int not in (0, None)) or payload.get("success") is False:
            raise Web3ApiError(code_int, str(payload.get("msg") or payload.get("message") or "error"), resp.status_code)
        return payload.get("data")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)

    # ------------------------------------------------------------------ Market API (products/market-api/introduction)
    def get_candles(self, binance_chain_id: str, token_contract_address: str, bar: str = "1h",
                    limit: int = 100, after: Optional[int] = None, before: Optional[int] = None) -> list[list[float]]:
        """GET /api/v1/dex/market/candles. Rows: [open, high, low, close, volume, timestamp_ms, tradeCount]."""
        return self.get("/api/v1/dex/market/candles", binanceChainId=binance_chain_id,
                        tokenContractAddress=token_contract_address, bar=bar, after=after, before=before,
                        limit=limit) or []

    def get_token_price(self, tokens: Sequence[dict[str, str]]) -> list[dict]:
        """POST /api/v1/dex/market/price (batch <= 100). Items: {binanceChainId, tokenContractAddress}."""
        return self.post("/api/v1/dex/market/price", list(tokens)) or []

    def get_rwa_price(self, binance_chain_id: str, token_contract_addresses: Sequence[str]) -> list[dict]:
        """GET /api/v1/dex/market/rwa/price -> [{tokenContractAddress, tokenPrice, referencePrice, ...}]."""
        return self.get("/api/v1/dex/market/rwa/price", binanceChainId=binance_chain_id,
                        tokenContractAddresses=",".join(token_contract_addresses)) or []

    def search_rwa(self, keyword: str, platform_id: Optional[str] = None) -> list[dict]:
        """GET /api/v1/dex/market/rwa/search -> [{ticker, companyName, assets:[{platformId, binanceChainId,
        tokenContractAddress, tokenSymbol, assetType}]}]. platform_id: 'ondo' | 'bstock'."""
        return self.get("/api/v1/dex/market/rwa/search", keyword=keyword, platformId=platform_id) or []

    # ------------------------------------------------------------------ Trading API (products/trading-api/*)
    def get_quote(self, binance_chain_id: str, from_token_address: str, to_token_address: str, amount: str,
                  user_wallet_address: Optional[str] = None, vendor: Optional[str] = None) -> list[dict]:
        """GET /api/v1/dex/aggregator/quote. `amount` in the sell token's smallest unit. Each route has its own
        `quoteId` (TTL ~30s) and `executionMode` (SWAP | RFQ). userWalletAddress is required for RFQ routes."""
        data = self.get("/api/v1/dex/aggregator/quote", binanceChainId=binance_chain_id, amount=amount,
                        fromTokenAddress=from_token_address, toTokenAddress=to_token_address,
                        userWalletAddress=user_wallet_address, vendor=vendor)
        if isinstance(data, dict):  # be lenient: single route object
            data = [data]
        return data or []

    def build_swap(self, binance_chain_id: str, from_token_address: str, to_token_address: str, amount: str,
                   user_wallet_address: str, quote_id: str, slippage_percent: Optional[str] = None) -> dict:
        """GET /api/v1/dex/aggregator/swap -> {tx | rfq, executionMode, routerResult}. Exactly one of
        slippagePercent / autoSlippage must be sent; params must match the cached quote (else 40462)."""
        return self.get("/api/v1/dex/aggregator/swap", binanceChainId=binance_chain_id, amount=amount,
                        fromTokenAddress=from_token_address, toTokenAddress=to_token_address,
                        userWalletAddress=user_wallet_address, quoteId=quote_id,
                        slippagePercent=slippage_percent,
                        autoSlippage=None if slippage_percent is not None else "true") or {}

    def approve_transaction(self, binance_chain_id: str, token_contract_address: str, approve_amount: str,
                            vendor: Optional[str] = None) -> list[dict]:
        """GET /api/v1/dex/aggregator/approve-transaction (vendor required for RFQ = vendorName from /quote)."""
        data = self.get("/api/v1/dex/aggregator/approve-transaction", binanceChainId=binance_chain_id,
                        tokenContractAddress=token_contract_address, approveAmount=approve_amount, vendor=vendor)
        if isinstance(data, dict):
            data = [data]
        return data or []

    def submit_rfq_order(self, request_id: str, user_signature: str, vendor: str, quote_id: str,
                         signing_scheme: Optional[str] = None) -> dict:
        """POST /api/v1/dex/aggregator/order/submit. quote_id = rfq.orderId from /swap. requestId is idempotent 30 min."""
        body = {"requestId": request_id, "userSignature": user_signature, "vendor": vendor, "quoteId": quote_id}
        if signing_scheme:
            body["signingScheme"] = signing_scheme
        return self.post("/api/v1/dex/aggregator/order/submit", body) or {}

    def get_rfq_order(self, order_id: str) -> dict:
        """GET /api/v1/dex/aggregator/order/{orderId}: PENDING_VENDOR|PENDING_ONCHAIN -> FILLED|FAILED|EXPIRED|CANCELLED."""
        return self.get(f"/api/v1/dex/aggregator/order/{order_id}") or {}

    # ------------------------------------------------------------------ Transaction / Wallet API
    def broadcast(self, binance_chain_id: str, signed_transaction: str, address: str,
                  enable_mev_protection: bool = False) -> dict:
        """POST /api/v1/dex/pre-transaction/broadcast-transaction -> {txHash, orderId}."""
        return self.post("/api/v1/dex/pre-transaction/broadcast-transaction", {
            "binanceChainId": binance_chain_id, "signedTransaction": signed_transaction,
            "address": address, "enableMevProtection": enable_mev_protection,
        }) or {}

    def tx_detail(self, binance_chain_id: str, tx_hash: str) -> list[dict]:
        """GET /api/v1/dex/post-transaction/transaction-detail-by-txhash -> [{txStatus: pending|success|fail, ...}].
        An empty list right after broadcast means "not indexed yet"."""
        data = self.get("/api/v1/dex/post-transaction/transaction-detail-by-txhash",
                        binanceChainId=binance_chain_id, txHash=tx_hash)
        if isinstance(data, dict):
            data = [data]
        return data or []


PUBLIC_BASE_URL = "https://www.binance.com/bapi/defi"
PUBLIC_HEADERS = {"Accept-Encoding": "identity", "User-Agent": "binance-web3/1.1 (Skill)"}


class PublicRwaClient:
    """No-key public RWA endpoints used by the official binance-tokenized-securities-info skill (v1.1).
    They work from regions where the HMAC Web3 API returns 40304. Envelope: {code: "000000", data, success}."""

    def __init__(self, base_url: str = PUBLIC_BASE_URL, http: Optional[httpx.Client] = None,
                 timeout_s: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout_s, headers=PUBLIC_HEADERS)

    def _get(self, path: str, **params: Any) -> Any:
        resp = self._http.get(self._base + path, params={k: v for k, v in params.items() if v is not None},
                              headers=PUBLIC_HEADERS)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code >= 400 or not isinstance(payload, dict) or payload.get("success") is False:
            code = payload.get("code") if isinstance(payload, dict) else None
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            raise Web3ApiError(code, f"public RWA HTTP {resp.status_code}", resp.status_code)
        return payload.get("data")

    def rwa_dynamic(self, chain_id: str, contract_address: str) -> dict:
        """GET v2/.../rwa/dynamic/ai -> {tokenInfo{price, sharesMultiplier}, stockInfo{price}, statusInfo, limitInfo}."""
        return self._get("/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai",
                         chainId=chain_id, contractAddress=contract_address) or {}

    def asset_market_status(self, chain_id: str, contract_address: str) -> dict:
        """GET v1/.../rwa/asset/market/status/ai -> {openState, marketStatus, reasonCode, reasonMsg, nextOpenTime, ...}."""
        return self._get("/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai",
                         chainId=chain_id, contractAddress=contract_address) or {}

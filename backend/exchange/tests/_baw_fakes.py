"""Fake `baw` 1.10.0 runner. Payloads are shaped exactly like dist/index.js prints them with --json:
success -> stdout `{"success": true, "data": ...}` (indent 2), exit 0; error -> stdout
`{"success": false, "error": {"code", "name", "message", "data"?}}`, exit 1."""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Optional, Sequence

from backend.exchange.baw import RunResult

USDT = "0x55d398326f99059fF775485246999027B3197955"
NVDAB = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
NVDAON = "0xa9ee28c80f960b889dfbd1902055218cba016f75"
BNB_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ADDR = "0x1111111111111111111111111111111111111111"
TX = "0x" + "ab" * 32
ORDER_ID = "7712345678901234567"

STATUS_CONNECTED = {"status": "CONNECTED"}
ADDRESSES = {"addresses": [{"binanceChainId": "56", "chainName": "BSC", "address": ADDR},
                           {"binanceChainId": "1", "chainName": "Ethereum", "address": ADDR}]}
BALANCES = [  # `wallet balance --binanceChainId 56 --json`: data is a list
    {"symbol": "BNB", "address": BNB_NATIVE, "binanceChainId": "56", "balance": "0.012", "price": "600", "value": "7.2"},
    {"symbol": "USDT", "address": USDT, "binanceChainId": "56", "balance": "42.5", "price": "1", "value": "42.5"},
    {"symbol": "NVDAB", "address": NVDAB, "binanceChainId": "56", "balance": "0.1", "price": "185.2", "value": "18.52"},
]
SETTINGS = {  # `wallet settings --json` (keys exactly as the source's g({...}) call)
    "maxSigninDuration": "48", "dailyLimit": 50, "abnormalTxnHandling": "AutoReject", "inactiveSignoutDuration": "48",
    "tradeAllTokens": False, "predictionEnabled": False, "predictionDailyLimit": 0, "predictionQuotaUsed": 0,
    "predictionQuotaLeft": 0, "predictionQuotaDate": "2026-10-11", "defiDailyLimit": 0, "defiQuotaUsed": 0,
    "defiQuotaLeft": 0, "defiQuotaDate": "2026-10-11", "x402DailyLimit": 20, "x402QuotaUsed": 0, "x402QuotaLeft": 20,
    "x402QuotaDate": "2026-10-11", "developerModeQuotaUsed": None, "developerModeQuotaDate": None,
    "quotaUsed": 12.3, "quotaLeft": 37.7, "quotaDate": "2026-10-11", "devMode": None,
    "sessionExpireTime": "2026-10-13 07:00:00 (UTC+05:30)", "signInMaxTime": None, "inactiveSignOutTime": None,
}
LEFT_QUOTA = {"quotaUsed": 12.3, "quotaLeft": 37.7, "dailyLimit": 50, "date": "2026-10-11"}
UNLOCKED = {"status": "UNLOCKED"}
LOCKED = {"status": "LOCKED"}
QUOTE = {"fromCoinSymbol": "USDT", "fromCoinAmount": "1", "toCoinSymbol": "NVDAB", "toCoinAmount": "0.0054",
         "slippage": 0.01}
SWAP = {"orderId": ORDER_ID, "clientOrderId": "c-1"}


def order_row(status: str = "FINISHED", order_id: str = ORDER_ID, from_token: str = USDT, to_token: str = NVDAB,
              from_qty: str = "10", to_qty: Optional[str] = "0.054", tx: Optional[str] = TX) -> dict:
    """`market-order list --json` row (mapToOrderRecord)."""
    return {"orderType": "market", "orderId": order_id, "chain": "56", "fromToken": from_token, "fromTokenName": "USDT",
            "fromTokenQty": from_qty, "toToken": to_token, "toTokenName": "NVDAB", "toTokenActualQty": to_qty,
            "status": status, "slippage": "0.01", "txHash": tx, "bookTime": "2026-10-11 07:30:00",
            "updatedTime": "2026-10-11 07:30:05"}


def listing(*rows: dict) -> dict:
    return {"total": len(rows), "page": 1, "pageSize": 20, "list": list(rows)}


def tx_item(send_token: str = USDT, send_amt: str = "10", recv_token: str = NVDAB, recv_amt: str = "0.0539",
            fee_raw: str = "52000000000000", tx: str = TX) -> dict:
    """One `wallet tx-history --json` transaction (transformToJsonItem): amounts in display units, fee raw."""
    def ins(token: str, amt: str, sym: str) -> dict:
        return {"binanceChainId": "56", "amount": amt, "addressInfo": {"address": ADDR},
                "tokenInfo": {"binanceChainId": "56", "contractAddress": token, "symbol": sym, "decimals": 18}}
    return {"txType": "SWAP", "txHash": tx, "txTime": "2026-10-11 07:30:04", "binanceChainId": "56", "status": "confirmed",
            "txHashList": [{"binanceChainId": "56", "txHash": tx, "status": "confirmed",
                            "networkFee": {"binanceChainId": "56", "feeTokenAddress": BNB_NATIVE, "feeTokenSymbol": "BNB",
                                           "feeValue": fee_raw, "feeTokenDecimals": 18},
                            "instructions": {"send": [ins(send_token, send_amt, "S")],
                                             "receive": [ins(recv_token, recv_amt, "R")]}}]}


def ok(data: Any) -> RunResult:
    return RunResult(0, json.dumps({"success": True, "data": data}, indent=2))


def err(code: int, name: str, message: str, data: Optional[dict] = None) -> RunResult:
    e: dict = {"code": code, "name": name, "message": message}
    if data:
        e["data"] = data
    return RunResult(1, json.dumps({"success": False, "error": e}, indent=2))


Handler = Callable[[list[str]], RunResult]


class FakeBaw:
    """Routes argv to handlers by `group cmd`; records every call. Unknown commands fail the test."""

    def __init__(self, **over: Any) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.h: dict[str, Any] = {
            "wallet status": ok(STATUS_CONNECTED), "wallet address": ok(ADDRESSES), "wallet balance": ok(BALANCES),
            "wallet settings": ok(SETTINGS), "wallet left-quota": ok(LEFT_QUOTA), "wallet tx-lock": ok(UNLOCKED),
            "wallet tx-history": ok({"transactions": [tx_item()], "hasMore": False}),
            "market-order quote": ok(QUOTE), "market-order swap": ok(SWAP),
            "market-order list": lambda argv: ok(listing(order_row()) if "--orderId" in argv else listing()),
        }
        for k, v in over.items():  # wallet_status=..., market_order_swap=..., wallet_tx_lock=...
            for a, b in (("market_order", "market-order"), ("left_quota", "left-quota"), ("tx_lock", "tx-lock"),
                         ("tx_history", "tx-history")):
                k = k.replace(a, b)
            self.h[k.replace("_", " ", 1)] = v

    def set(self, key: str, value: Any) -> None:
        self.h[key] = value

    def __call__(self, argv: Sequence[str], timeout_s: float) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        self.timeouts.append(timeout_s)
        assert argv[-1] == "--json"
        key = " ".join(argv[1:3])
        h = self.h[key]
        if isinstance(h, list):  # sequence of responses, last one repeats
            h = h.pop(0) if len(h) > 1 else h[0]
        if callable(h):
            h = h(argv)
        if isinstance(h, BaseException):
            raise h
        return h

    def cmds(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]

    def argv(self, key: str) -> list[str]:
        return next(c for c in self.calls if " ".join(c[1:3]) == key)


def timeout() -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(["baw"], 45)

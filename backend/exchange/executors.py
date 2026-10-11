"""LIVE execution back-ends. Only used by BinanceWeb3Client in LIVE mode.

Two options:
1. AgenticWalletExecutor - drives the Binance Agentic Wallet CLI (`baw`, npm @binance/agentic-wallet).
   MPC keyless: we never hold a private key; limits/allow-lists are enforced by the user's Binance App settings.
   Docs: https://web3.binance.com/en/dev-docs/products/agentic-wallet/introduction and
   https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-agentic-wallet/references/market-order.md
2. Web3ApiExecutor - Trading API quote -> /swap -> sign locally -> /broadcast-transaction (SWAP) or
   sign rfq.typedDataToSign (EIP-712) -> /order/submit -> poll /order/{id} (RFQ).
   Docs: https://web3.binance.com/en/dev-docs/products/trading-api/integration-flow
   Requires a WalletSigner (you hold the key). The APIs never receive the key.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from backend.contracts import ExecutionPath, ExecutionResult, OrderRequest, OrderSide, OrderStatus, Quote, TradingMode, utc_now

from .errors import QUOTE_EXPIRED, ExchangeError, Web3ApiError
from .tokens import from_base_units
from .web3_api import Web3ApiClient


@dataclass
class RouteContext:
    """What we remember about a Binance quote, keyed by our Quote.quote_id."""

    chain_id: str
    from_token: str
    to_token: str
    from_decimals: int
    to_decimals: int
    amount: str  # smallest units of from_token
    from_qty: float  # human units of from_token
    binance_quote_id: Optional[str] = None
    vendor: Optional[str] = None
    execution_mode: str = "SWAP"
    raw_route: dict = field(default_factory=dict)


def _result(order: OrderRequest, path: ExecutionPath, status: OrderStatus, **kw: Any) -> ExecutionResult:
    return ExecutionResult(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side,
                           path=path, mode=TradingMode.LIVE, status=status, **kw)


def _fill_from_amounts(order: OrderRequest, ctx: RouteContext, to_raw: Any, fallback: Quote) -> tuple[float, float]:
    """(token quantity, USDT notional) from settled amounts; falls back to the quote."""
    from_h = ctx.from_qty
    to_h = from_base_units(to_raw, ctx.to_decimals) if to_raw not in (None, "") else None
    if to_h is None or to_h <= 0:
        return fallback.quantity, fallback.notional
    if order.side == OrderSide.BUY:  # spend USDT -> receive token
        return to_h, from_h
    return from_h, to_h


@runtime_checkable
class LiveExecutor(Protocol):
    def execute(self, order: OrderRequest, quote: Quote, ctx: RouteContext, path: ExecutionPath) -> ExecutionResult: ...


@runtime_checkable
class WalletSigner(Protocol):
    """Local signer (you hold the key). Implement with eth-account / a hardware wallet / a KMS."""

    @property
    def address(self) -> str: ...

    def sign_transaction(self, tx: dict, chain_id: int) -> str:
        """Return the raw signed tx hex (0x...). `tx` is the Trading API `data.tx` / approve item."""
        ...

    def sign_typed_data(self, typed_data: dict) -> str:
        """eth_signTypedData_v4 -> 0x + 65-byte hex signature."""
        ...


# --------------------------------------------------------------------------- Web3 API + own signer
class Web3ApiExecutor:
    TERMINAL_RFQ = {"FILLED", "FAILED", "EXPIRED", "CANCELLED"}

    def __init__(self, api: Web3ApiClient, signer: WalletSigner, poll_interval_s: float = 2.0,
                 timeout_s: float = 90.0, mev_protection: bool = True, approve: bool = True,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.api, self.signer = api, signer
        self.poll, self.timeout, self.mev, self.approve = poll_interval_s, timeout_s, mev_protection, approve
        self._sleep, self._now = sleep, monotonic

    def execute(self, order: OrderRequest, quote: Quote, ctx: RouteContext, path: ExecutionPath) -> ExecutionResult:
        try:
            if self.approve:
                self._approve(ctx, vendor=ctx.vendor if path == ExecutionPath.RFQ else None)
            built = self.api.build_swap(ctx.chain_id, ctx.from_token, ctx.to_token, ctx.amount, self.signer.address,
                                        ctx.binance_quote_id or "", slippage_percent=f"{order.slippage_bps / 100:g}")
            if path == ExecutionPath.RFQ:
                return self._rfq(order, quote, ctx, built)
            return self._swap(order, quote, ctx, built)
        except Web3ApiError as e:
            status = OrderStatus.EXPIRED if e.code == QUOTE_EXPIRED else OrderStatus.REJECTED
            return _result(order, path, status, error=str(e))
        except ExchangeError as e:
            return _result(order, path, OrderStatus.FAILED, error=str(e))

    def _approve(self, ctx: RouteContext, vendor: Optional[str]) -> None:
        if ctx.from_token.lower().startswith("0xeeeeeeee"):
            return  # native token: no ERC-20 approval
        items = self.api.approve_transaction(ctx.chain_id, ctx.from_token, ctx.amount, vendor=vendor)
        for item in items:
            if not item.get("data"):
                continue
            tx = {"from": self.signer.address, "to": ctx.from_token, "data": item["data"], "value": "0",
                  "gas": item.get("gasLimit"), "gasPrice": item.get("gasPrice")}
            raw = self.signer.sign_transaction(tx, int(ctx.chain_id))
            sent = self.api.broadcast(ctx.chain_id, raw, self.signer.address, enable_mev_protection=False)
            if self._wait_tx(ctx.chain_id, sent.get("txHash", "")) != "success":
                raise ExchangeError("ERC-20 approval transaction did not succeed")

    def _wait_tx(self, chain_id: str, tx_hash: str) -> str:
        deadline = self._now() + self.timeout
        while True:
            rows = self.api.tx_detail(chain_id, tx_hash) if tx_hash else []
            status = (rows[0].get("txStatus") if rows else None) or "pending"
            if status in ("success", "fail") or self._now() >= deadline:
                return status
            self._sleep(self.poll)

    def _swap(self, order: OrderRequest, quote: Quote, ctx: RouteContext, built: dict) -> ExecutionResult:
        tx = built.get("tx")
        if not tx:
            return _result(order, ExecutionPath.SWAP, OrderStatus.REJECTED,
                           error=f"route executionMode={built.get('executionMode')}: no tx to sign (use execute_rfq)")
        raw = self.signer.sign_transaction(tx, int(ctx.chain_id))
        sent = self.api.broadcast(ctx.chain_id, raw, self.signer.address, enable_mev_protection=self.mev)
        tx_hash = sent.get("txHash")
        status = self._wait_tx(ctx.chain_id, tx_hash or "")
        if status == "success":
            qty, notional = _fill_from_amounts(order, ctx, None, quote)
            return _result(order, ExecutionPath.SWAP, OrderStatus.FILLED, filled_quantity=qty,
                           avg_price=notional / qty if qty else quote.price, notional=notional, fee=quote.fee,
                           tx_hash=tx_hash)
        err = "on-chain swap failed" if status == "fail" else f"UNCONFIRMED after {self.timeout:.0f}s - check tx_hash"
        return _result(order, ExecutionPath.SWAP, OrderStatus.FAILED, tx_hash=tx_hash, error=err)

    def _rfq(self, order: OrderRequest, quote: Quote, ctx: RouteContext, built: dict) -> ExecutionResult:
        rfq = built.get("rfq")
        if not rfq or not rfq.get("typedDataToSign"):
            return _result(order, ExecutionPath.RFQ, OrderStatus.REJECTED,
                           error=f"route executionMode={built.get('executionMode')}: no rfq typed data (use execute_swap)")
        typed = rfq["typedDataToSign"]
        typed = json.loads(typed) if isinstance(typed, str) else typed
        signature = self.signer.sign_typed_data(typed)
        # Deterministic idempotency key per client order: retries reuse it (order/submit is idempotent for 30 min).
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"teamwork:{order.client_order_id}"))
        sub = self.api.submit_rfq_order(request_id, signature, rfq.get("vendor") or ctx.vendor or "",
                                        rfq.get("orderId") or "", rfq.get("signingScheme"))
        order_id = sub.get("orderId")
        deadline = self._now() + self.timeout
        st: dict = sub
        while (st.get("status") or "") not in self.TERMINAL_RFQ and self._now() < deadline:
            self._sleep(self.poll)
            st = self.api.get_rfq_order(order_id) if order_id else {}
        status = st.get("status") or "PENDING"
        if status == "FILLED":
            qty, notional = _fill_from_amounts(order, ctx, st.get("toAmount"), quote)
            return _result(order, ExecutionPath.RFQ, OrderStatus.FILLED, filled_quantity=qty,
                           avg_price=notional / qty if qty else quote.price, notional=notional,
                           tx_hash=st.get("txHash"), order_id=order_id or uuid.uuid4().hex)
        mapped = OrderStatus.EXPIRED if status == "EXPIRED" else OrderStatus.FAILED
        err = f"RFQ order {status}" if status in self.TERMINAL_RFQ else f"UNCONFIRMED RFQ order {order_id} ({status})"
        return _result(order, ExecutionPath.RFQ, mapped, error=err)


# --------------------------------------------------------------------------- Binance Agentic Wallet CLI
Runner = Callable[[Sequence[str]], str]


def subprocess_runner(timeout_s: float = 120.0) -> Runner:
    def run(args: Sequence[str]) -> str:
        proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout_s, check=False)
        return proc.stdout or proc.stderr

    return run


class AgenticWalletExecutor:
    """`baw market-order swap ... --json` then poll `baw market-order list --orderId <id> --json` to FINISHED/FAILED.

    The CLI re-quotes and routes itself (SWAP or RFQ); we pass our slippage bound. A returned orderId only means
    *submitted* (market-order.md "Mandatory: an orderId is NOT a completed swap").
    """

    def __init__(self, runner: Optional[Runner] = None, baw_bin: str = "baw", poll_interval_s: float = 3.0,
                 timeout_s: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.runner = runner or subprocess_runner()
        self.bin = baw_bin
        self.poll, self.timeout, self._sleep, self._now = poll_interval_s, timeout_s, sleep, monotonic

    @staticmethod
    def available(baw_bin: str = "baw") -> bool:
        return shutil.which(baw_bin) is not None

    def _call(self, *args: str) -> dict:
        out = self.runner([self.bin, *args, "--json"])
        try:
            payload = json.loads(out)
        except (TypeError, ValueError):
            raise ExchangeError(f"baw returned non-JSON output: {str(out)[:200]}")
        if not payload.get("success"):
            err = payload.get("error") or {}
            raise ExchangeError(f"baw error {err.get('code')} {err.get('name')}: {err.get('message')}", err.get("code"))
        return payload.get("data") or {}

    def status(self) -> str:
        return str(self._call("wallet", "status").get("status", "UNKNOWN"))

    def execute(self, order: OrderRequest, quote: Quote, ctx: RouteContext, path: ExecutionPath) -> ExecutionResult:
        try:
            if self.status() != "CONNECTED":
                return _result(order, path, OrderStatus.REJECTED,
                               error="Agentic Wallet not connected: run `baw auth signin` and confirm in the Binance App")
            data = self._call("market-order", "swap", "--fromTokenQty", f"{ctx.from_qty:.18g}",
                              "--fromToken", ctx.from_token, "--toToken", ctx.to_token,
                              "--binanceChainId", ctx.chain_id, "--slippage", f"{order.slippage_bps / 100:g}")
            order_id = str(data.get("orderId") or "")
            if not order_id:
                return _result(order, path, OrderStatus.FAILED, error="baw swap returned no orderId")
            deadline = self._now() + self.timeout
            row: dict = {}
            while True:
                listing = self._call("market-order", "list", "--orderId", order_id)
                rows = listing.get("list") or []
                row = rows[0] if rows else {}
                if row.get("status") in ("FINISHED", "FAILED") or self._now() >= deadline:
                    break
                self._sleep(self.poll)
            if row.get("status") == "FINISHED":
                # The list response has no received amount; report the quote's estimate (bounded by slippage).
                return _result(order, path, OrderStatus.FILLED, filled_quantity=quote.quantity, avg_price=quote.price,
                               notional=quote.notional, fee=quote.fee, tx_hash=row.get("txHash"), order_id=order_id)
            if row.get("status") == "FAILED":
                return _result(order, path, OrderStatus.FAILED, tx_hash=row.get("txHash"), order_id=order_id,
                               error="Agentic Wallet market order FAILED")
            return _result(order, path, OrderStatus.FAILED, order_id=order_id,
                           error=f"UNCONFIRMED: Agentic Wallet order {order_id} still PENDING after {self.timeout:.0f}s")
        except ExchangeError as e:
            return _result(order, path, OrderStatus.REJECTED, error=str(e))

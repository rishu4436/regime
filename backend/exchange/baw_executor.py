"""LIVE executor on the Binance Agentic Wallet CLI (`baw` 1.10.0) - real funds on BNB Smart Chain.

Flow for one order (single-flight, fail-closed):
  gate (LIVE_TRADING_ENABLED + BINANCE_LIVE_ENABLED + RISK_ALLOW_LIVE all "true") -> per-order hard cap
  (BINANCE_LIVE_MAX_ORDER_USDT, default 25) -> process-wide lock -> resolve open intents -> `wallet status` CONNECTED
  -> `wallet tx-lock` UNLOCKED -> no PENDING `market-order list` -> persist intent (JSON) -> `market-order swap
  --slippage <pct> --mev true` (45 s) -> poll `market-order list --orderId` every 3 s for up to 120 s ->
  FINISHED: real fill + gas from `wallet tx-history` | FAILED | still PENDING -> PENDING_CONFIRMATION.
A swap whose outcome is unknown (our timeout, ORDER_TIMEOUT, network/503) is NEVER retried: we reconcile with
`market-order list --fromToken --toToken --startTime` and otherwise return PENDING_CONFIRMATION/outcome_unknown
with the intent kept open (reconcile() resolves it later; new swaps are refused until then).

Every non-filled result carries `error="[<reason>] <message>"` (reason codes in backend.exchange.baw); extras that
the contract has no field for (explorer_url, route, real_funds, wallet_order_id, reason, gas) are in
`executor.extras[client_order_id]` / `executor.last_extras`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from backend.contracts import ExecutionPath, ExecutionResult, OrderRequest, OrderSide, OrderStatus, Quote, TradingMode, utc_now

from . import baw as B
from .baw import BSC_CHAIN_ID, BawCli, BawError
from .executors import RouteContext
from .tokens import QUOTE_ASSETS

EXPLORER_TX = "https://bscscan.com/tx/{}"
GATE_ENV = ("LIVE_TRADING_ENABLED", "BINANCE_LIVE_ENABLED", "RISK_ALLOW_LIVE")
DEFAULT_MAX_ORDER_USDT = 25.0
DEFAULT_MAX_SLIPPAGE_PCT = 1.0
DEFAULT_BNB_GAS_RESERVE = 0.002
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 120.0

_PROCESS_LOCK = threading.Lock()  # one swap in flight per process


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _float_env(name: str, default: float, env: Optional[Mapping[str, str]] = None) -> float:
    try:
        v = float(str(_env(env).get(name, "")).strip() or default)
        return v if v >= 0 else default
    except ValueError:
        return default


def live_gate(env: Optional[Mapping[str, str]] = None, require_risk_allow_live: bool = True) -> tuple[bool, list[str]]:
    """(allowed, missing switches). Each switch must be exactly 'true' (case-insensitive)."""
    names = GATE_ENV if require_risk_allow_live else GATE_ENV[:2]
    missing = [f"{n}=true" for n in names if str(_env(env).get(n, "")).strip().lower() != "true"]
    return (not missing, missing)


def max_order_usdt(env: Optional[Mapping[str, str]] = None) -> float:
    return _float_env("BINANCE_LIVE_MAX_ORDER_USDT", DEFAULT_MAX_ORDER_USDT, env)


def max_slippage_pct(env: Optional[Mapping[str, str]] = None) -> float:
    return min(100.0, _float_env("BINANCE_LIVE_MAX_SLIPPAGE_PCT", DEFAULT_MAX_SLIPPAGE_PCT, env))


def explorer_url(tx_hash: Optional[str]) -> Optional[str]:
    return EXPLORER_TX.format(tx_hash) if tx_hash else None


def route_for(symbol: str) -> str:
    """baw routes Ondo (`...on`, RWA type 1) through its Ondo RFQ endpoint, everything else through place-order."""
    return "ONDO_RFQ" if symbol.endswith("on") else "SWAP"


_REASON_RE = re.compile(r"^\[([a-z_]+)\]")


def result_reason(result: ExecutionResult) -> Optional[str]:
    """Reason code of a non-filled result (parsed from the `[reason] ...` error prefix)."""
    m = _REASON_RE.match(result.error or "")
    return m.group(1) if m else None


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- intent journal
def default_intent_path() -> Path:
    raw = os.getenv("BAW_INTENT_FILE")
    return Path(raw) if raw else Path.home() / ".teamwork" / "baw_intents.json"


class IntentStore:
    """Order intents persisted BEFORE `swap` is called (atomic JSON writes). States:
    SUBMITTING -> SUBMITTED -> FINISHED | FAILED | PENDING | UNKNOWN; REJECTED when baw refused before submission."""

    OPEN = {"SUBMITTING", "SUBMITTED", "PENDING", "UNKNOWN"}

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else default_intent_path()
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {}
        except ValueError:
            bad = self.path.with_suffix(f".corrupt-{_now_ms()}.json")
            self.path.replace(bad)
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, rec: dict) -> dict:
        with self._lock:
            data = self.load()
            rec = {**rec, "updated_ms": _now_ms()}
            data[rec["client_order_id"]] = rec
            self._save(data)
            return rec

    def update(self, client_order_id: str, **kw: Any) -> dict:
        with self._lock:
            data = self.load()
            rec = {**data.get(client_order_id, {"client_order_id": client_order_id}), **kw, "updated_ms": _now_ms()}
            data[client_order_id] = rec
            self._save(data)
            return rec

    def get(self, client_order_id: str) -> Optional[dict]:
        return self.load().get(client_order_id)

    def open_intents(self) -> list[dict]:
        return [r for r in self.load().values() if r.get("state") in self.OPEN]


# --------------------------------------------------------------------------- executor
class BawExecutor:
    """`LiveExecutor` (execute(order, quote, ctx, path)) + wallet reads, on the Agentic Wallet CLI."""

    def __init__(self, cli: Optional[BawCli] = None, intents: Optional[IntentStore] = None,
                 env: Optional[Mapping[str, str]] = None, require_risk_allow_live: bool = True,
                 max_order_usdt: Optional[float] = None, max_sell_usdt: Optional[float] = None,
                 max_slippage_pct: Optional[float] = None, bnb_gas_reserve: Optional[float] = None,
                 poll_interval_s: float = POLL_INTERVAL_S, poll_timeout_s: float = POLL_TIMEOUT_S,
                 chain_id: str = BSC_CHAIN_ID, wallet_ttl_s: float = 15.0,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 lock: Optional[threading.Lock] = None) -> None:
        self.cli = cli or BawCli()
        self.intents = intents or IntentStore()
        self._envmap = env
        self.require_risk_allow_live = require_risk_allow_live
        self.max_order_usdt = max_order_usdt if max_order_usdt is not None else globals()["max_order_usdt"](env)
        # Sells are exits: allow a position that grew above the buy cap to be closed, but still bound it.
        self.max_sell_usdt = (max_sell_usdt if max_sell_usdt is not None
                              else _float_env("BINANCE_LIVE_MAX_SELL_USDT", 2 * self.max_order_usdt, env))
        self.max_slippage_pct = (max_slippage_pct if max_slippage_pct is not None
                                 else globals()["max_slippage_pct"](env))
        self.bnb_gas_reserve = (bnb_gas_reserve if bnb_gas_reserve is not None
                                else _float_env("BINANCE_BNB_GAS_RESERVE", DEFAULT_BNB_GAS_RESERVE, env))
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s
        self.chain_id = chain_id
        self.wallet_ttl_s = wallet_ttl_s
        self._sleep, self._mono = sleep, monotonic
        self._lock = lock or _PROCESS_LOCK
        self.extras: dict[str, dict] = {}
        self.last_extras: dict = {}
        self._wallet_cache: Optional[tuple[float, dict]] = None
        self._bnb_price: Optional[float] = None

    # ------------------------------------------------------------------ runtime limits
    def set_live_limits(self, max_order_usdt: float, max_sell_usdt: Optional[float] = None) -> dict:
        """Change the per-BUY cap at runtime (Bot 6's PUT /api/settings/limits; same value as risk and engine).
        Validated: > 0 and <= BINANCE_LIVE_HARD_MAX_ORDER_USDT when that is set (ValueError otherwise; nothing changes).
        The exit (SELL) cap becomes `max_sell_usdt` if given, else max(BINANCE_LIVE_MAX_SELL_USDT if set, 2 x buy cap),
        so a position bought at the new cap can always be closed. Takes effect on the next order."""
        from .limits import hard_max_order_usdt, validate_max_order_usdt

        ceiling = hard_max_order_usdt(self._envmap)
        buy = validate_max_order_usdt(max_order_usdt, ceiling)
        if max_sell_usdt is not None:
            sell = validate_max_order_usdt(max_sell_usdt, None, "max_sell_usdt")
            if sell < buy:
                raise ValueError(f"max_sell_usdt {sell:.2f} must be >= max_order_usdt {buy:.2f} (exits must fit)")
        else:
            env_sell = str(_env(self._envmap).get("BINANCE_LIVE_MAX_SELL_USDT", "")).strip()
            sell = max(_float_env("BINANCE_LIVE_MAX_SELL_USDT", 0.0, self._envmap) if env_sell else 0.0, 2 * buy)
        self.max_order_usdt, self.max_sell_usdt = buy, sell
        self._wallet_cache = None  # wallet snapshot carries per_order_cap_usdt
        return self.live_limits()

    def live_limits(self) -> dict:
        from .limits import hard_max_order_usdt

        return {"max_order_usdt": self.max_order_usdt, "max_sell_usdt": self.max_sell_usdt,
                "default_max_order_usdt": max_order_usdt(self._envmap),
                "hard_max_order_usdt": hard_max_order_usdt(self._envmap)}

    # ------------------------------------------------------------------ gate
    def gate(self) -> tuple[bool, list[str]]:
        return live_gate(self._envmap, self.require_risk_allow_live)

    # ------------------------------------------------------------------ LiveExecutor
    def execute(self, order: OrderRequest, quote: Quote, ctx: RouteContext, path: ExecutionPath,
                max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        qty = order.quantity if (order.side == OrderSide.SELL and order.quantity) else ctx.from_qty
        notional = ctx.from_qty if order.side == OrderSide.BUY else (quote.notional or order.notional)
        return self.execute_order(order, from_token=ctx.from_token, to_token=ctx.to_token, from_qty=qty,
                                  notional_usdt=notional, path=path, quote=quote, chain_id=ctx.chain_id or self.chain_id,
                                  max_slippage_pct=max_slippage_pct)

    def execute_order(self, order: OrderRequest, from_token: str, to_token: str, from_qty: float,
                      notional_usdt: float, path: ExecutionPath = ExecutionPath.SWAP, quote: Optional[Quote] = None,
                      chain_id: Optional[str] = None, max_slippage_pct: Optional[float] = None) -> ExecutionResult:
        """Submit one market order through baw. `from_qty` is in display units (USDT for buys, token for sells).
        `max_slippage_pct` (percent 0-100) overrides BINANCE_LIVE_MAX_SLIPPAGE_PCT for this call (e.g. risk's cap)."""
        chain = str(chain_id or self.chain_id)
        slip = self.max_slippage_pct if max_slippage_pct is None else float(max_slippage_pct)
        ex: dict[str, Any] = {"real_funds": True, "provider": "baw", "route": route_for(order.symbol),
                              "chain_id": int(chain) if chain.isdigit() else chain, "slippage_pct": slip,
                              "wallet_order_id": None, "explorer_url": None, "reason": None}
        self.extras[order.client_order_id] = ex
        self.last_extras = ex

        def out(status: OrderStatus, reason: Optional[str] = None, msg: str = "", **kw: Any) -> ExecutionResult:
            ex["reason"] = reason
            if kw.get("tx_hash"):
                ex["explorer_url"] = explorer_url(kw["tx_hash"])
            if kw.get("order_id"):
                ex["wallet_order_id"] = kw["order_id"]
            if reason:
                kw["error"] = f"[{reason}] {msg}".strip()
            return ExecutionResult(client_order_id=order.client_order_id, symbol=order.symbol, side=order.side,
                                   path=path, mode=TradingMode.LIVE, status=status, **kw)

        ok, missing = self.gate()
        if not ok:
            return out(OrderStatus.REJECTED, B.LIVE_DISABLED, "LIVE execution refused, missing: " + ", ".join(missing))
        if order.mode != TradingMode.LIVE:
            return out(OrderStatus.REJECTED, B.LIVE_DISABLED, "OrderRequest.mode must be LIVE")
        if not (0 <= slip <= 100):
            return out(OrderStatus.REJECTED, B.SLIPPAGE_EXCEEDED, f"invalid slippage {slip}%")
        cap = self.max_order_usdt if order.side == OrderSide.BUY else self.max_sell_usdt
        if notional_usdt > cap + 1e-9:
            env_name = "BINANCE_LIVE_MAX_ORDER_USDT" if order.side == OrderSide.BUY else "BINANCE_LIVE_MAX_SELL_USDT"
            return out(OrderStatus.REJECTED, B.ORDER_CAP_EXCEEDED,
                       f"{order.side.value} {notional_usdt:.2f} USDT > {env_name}={cap:.2f}")
        if from_qty <= 0:
            return out(OrderStatus.REJECTED, B.AMOUNT_OUT_OF_RANGE, "quantity must be > 0")
        if not self._lock.acquire(blocking=False):
            return out(OrderStatus.REJECTED, B.ORDER_IN_FLIGHT, "another LIVE swap is in flight in this process")
        try:
            return self._locked(order, from_token, to_token, from_qty, slip, chain, quote, out, ex)
        finally:
            self._lock.release()

    def _locked(self, order, from_token, to_token, from_qty, slip, chain, quote, out, ex) -> ExecutionResult:
        cid = order.client_order_id
        # 1. preflight (all reads; any failure -> fail closed, nothing submitted)
        try:
            self.reconcile()
            still_open = [r for r in self.intents.open_intents() if r.get("client_order_id") != cid]
            if still_open:
                ids = ", ".join(f"{r['client_order_id']}({r.get('state')}, order {r.get('order_id')})" for r in still_open)
                return out(OrderStatus.REJECTED, B.ORDER_IN_FLIGHT, f"unresolved LIVE order intent(s): {ids}; "
                           "check the Binance App, then call executor.reconcile() or resolve_intent()")
            status = self.cli.status()
            if status != "CONNECTED":
                reason = B.SESSION_EXPIRED if status == "UNCONNECTED" else B.WALLET_NOT_READY
                return out(OrderStatus.REJECTED, reason, f"Agentic Wallet status {status}: re-sign-in needed "
                           "(`baw auth signin`, scan the QR in the Binance App)")
            lock = self.cli.tx_lock(chain)
            if lock != "UNLOCKED":
                return out(OrderStatus.REJECTED, B.TX_LOCKED, f"wallet tx-lock is {lock} (a transaction or an App "
                           "confirmation is in progress)")
            pending = self.cli.list_orders(status="PENDING", chain_id=chain)
            if pending:
                return out(OrderStatus.REJECTED, B.ORDER_IN_FLIGHT,
                           f"{len(pending)} PENDING market order(s) in the wallet, e.g. {pending[0].get('orderId')}")
        except BawError as e:
            return out(OrderStatus.REJECTED, e.reason, f"preflight: {e.args[0]}")
        except subprocess.TimeoutExpired:
            return out(OrderStatus.REJECTED, B.BAW_TIMEOUT, "preflight read timed out")

        # 2. persist intent BEFORE submitting
        started = _now_ms()
        self.intents.put({"client_order_id": cid, "state": "SUBMITTING", "symbol": order.symbol,
                          "side": order.side.value, "from_token": from_token, "to_token": to_token,
                          "from_qty": B.fmt_qty(from_qty), "slippage_pct": slip, "chain_id": chain,
                          "created_ms": started, "order_id": None, "tx_hash": None})
        ex["intent_file"] = str(self.intents.path)

        # 3. submit (never retried)
        order_id: Optional[str] = None
        try:
            data = self.cli.swap(from_token, to_token, from_qty, slip, chain_id=chain, mev=True)
            order_id = str(data.get("orderId") or "") or None
            if not order_id:
                self.intents.update(cid, state="UNKNOWN", note="swap returned no orderId")
                order_id = self._find_submitted(from_token, to_token, started, chain)
                if not order_id:
                    return out(OrderStatus.PENDING_CONFIRMATION, B.OUTCOME_UNKNOWN,
                               "baw swap returned no orderId; intent kept open, reconcile before trading again")
        except subprocess.TimeoutExpired:
            self.intents.update(cid, state="UNKNOWN", note="swap submit timed out")
            order_id = self._find_submitted(from_token, to_token, started, chain)
            if not order_id:
                return out(OrderStatus.PENDING_CONFIRMATION, B.OUTCOME_UNKNOWN,
                           f"swap submit timed out after {self.cli.swap_timeout_s:.0f}s and no order was found yet; "
                           "intent kept open - NOT retried")
        except BawError as e:
            if e.order_id:  # server attached an orderId (e.g. ORDER_API_ERROR after placement): follow it
                order_id = e.order_id
                self.intents.update(cid, state="SUBMITTED", order_id=order_id, note=str(e))
                if e.reason == B.APP_CONFIRMATION_REQUIRED:
                    return out(OrderStatus.PENDING_CONFIRMATION, e.reason, f"{e.args[0]} - confirm in the Binance "
                               "App (wallet is tx-locked until then)", order_id=order_id)
            elif e.ambiguous:
                self.intents.update(cid, state="UNKNOWN", note=str(e))
                order_id = self._find_submitted(from_token, to_token, started, chain)
                if not order_id:
                    return out(OrderStatus.PENDING_CONFIRMATION, B.OUTCOME_UNKNOWN,
                               f"{e.args[0]}; outcome unknown, intent kept open - NOT retried")
            else:
                self.intents.update(cid, state="REJECTED", reason=e.reason, note=str(e))
                status = OrderStatus.EXPIRED if e.reason == B.QUOTE_EXPIRED else (
                    OrderStatus.FAILED if e.reason == B.ORDER_FAILED else OrderStatus.REJECTED)
                if e.reason == B.APP_CONFIRMATION_REQUIRED:
                    status = OrderStatus.PENDING_CONFIRMATION
                return out(status, e.reason, e.args[0])

        self.intents.update(cid, state="SUBMITTED", order_id=order_id)
        ex["wallet_order_id"] = order_id
        # 4. poll
        row = self._poll(order_id)
        return self._finish(order, cid, order_id, row, from_token, to_token, from_qty, started, chain, quote, out, ex)

    # ------------------------------------------------------------------ helpers
    def _find_submitted(self, from_token: str, to_token: str, started_ms: int, chain: str) -> Optional[str]:
        """After an ambiguous submit: look for the order baw may have created (newest matching since start-5s)."""
        for _ in range(3):
            try:
                rows = self.cli.list_orders(from_token=from_token, to_token=to_token, start_ms=started_ms - 5000,
                                            chain_id=chain)
                if rows:
                    return str(rows[0].get("orderId") or "") or None
            except (BawError, subprocess.TimeoutExpired):
                pass
            self._sleep(self.poll_interval_s)
        return None

    def _poll(self, order_id: str) -> dict:
        deadline = self._mono() + self.poll_timeout_s
        row: dict = {}
        while True:
            try:
                rows = self.cli.list_orders(order_id=order_id)
                row = rows[0] if rows else row
            except subprocess.TimeoutExpired:
                pass
            except BawError as e:
                row = {**row, "_last_error": str(e), "_last_reason": e.reason}
                if e.reason == B.SESSION_EXPIRED:
                    return row
            if str(row.get("status") or "").upper() in ("FINISHED", "FAILED") or self._mono() >= deadline:
                return row
            self._sleep(self.poll_interval_s)

    def _finish(self, order, cid, order_id, row, from_token, to_token, from_qty, started, chain, quote, out, ex):
        st = str(row.get("status") or "").upper()
        tx_hash = row.get("txHash") or None
        if st == "FINISHED":
            fill = self._real_fill(order, row, tx_hash, from_token, to_token, from_qty, started, chain, quote)
            ex.update({k: fill[k] for k in ("gas_fee_bnb", "gas_fee_usd", "fill_source")})
            self.intents.update(cid, state="FINISHED", order_id=order_id, tx_hash=tx_hash,
                                filled_quantity=fill["qty"], notional=fill["notional"])
            return out(OrderStatus.FILLED, None, order_id=order_id, tx_hash=tx_hash, filled_quantity=fill["qty"],
                       avg_price=fill["notional"] / fill["qty"] if fill["qty"] else None, notional=fill["notional"],
                       fee=fill["fee"])
        if st == "FAILED":
            # baw's list row has no fail reason today; if a future one carries a message, honour slippage/min-receive.
            text = " ".join(str(v) for k, v in row.items() if k not in ("slippage",) and isinstance(v, str)).lower()
            reason = B.SLIPPAGE_EXCEEDED if B.classify(None, text) == B.SLIPPAGE_EXCEEDED else B.ORDER_FAILED
            self.intents.update(cid, state="FAILED", order_id=order_id, tx_hash=tx_hash, reason=reason)
            return out(OrderStatus.FAILED, reason, f"Agentic Wallet order {order_id} FAILED", order_id=order_id,
                       tx_hash=tx_hash)
        why = row.get("_last_reason")
        self.intents.update(cid, state="PENDING", order_id=order_id, tx_hash=tx_hash)
        reason = B.SESSION_EXPIRED if why == B.SESSION_EXPIRED else B.PENDING_CONFIRMATION
        return out(OrderStatus.PENDING_CONFIRMATION, reason,
                   f"order {order_id} still {st or 'UNKNOWN'} after {self.poll_timeout_s:.0f}s; keep it in flight and "
                   "call executor.reconcile() each tick", order_id=order_id, tx_hash=tx_hash)

    def _real_fill(self, order, row, tx_hash, from_token, to_token, from_qty, started, chain, quote) -> dict:
        sent = recv = gas_bnb = None
        source = "quote"
        if tx_hash:
            try:
                item = self._find_tx(tx_hash, started, chain)
                if item:
                    sent, recv, gas_bnb = _amounts(item, tx_hash, from_token, to_token)
                    source = "tx-history"
            except (BawError, subprocess.TimeoutExpired):
                pass
        if recv is None and row.get("toTokenActualQty") not in (None, ""):
            recv, source = float(row["toTokenActualQty"]), "market-order list"
        if sent is None:
            sent = float(row.get("fromTokenQty") or from_qty)
        if recv is None and quote is not None:
            recv = quote.quantity if order.side == OrderSide.BUY else quote.notional
        recv = recv or 0.0
        qty, notional = (recv, sent) if order.side == OrderSide.BUY else (sent, recv)
        gas_usd = gas_bnb * self._bnb_price if (gas_bnb is not None and self._bnb_price) else None
        fee = (quote.fee if quote else 0.0) + (gas_usd or 0.0)
        return {"qty": qty, "notional": notional, "fee": max(0.0, fee), "gas_fee_bnb": gas_bnb,
                "gas_fee_usd": gas_usd, "fill_source": source}

    def _find_tx(self, tx_hash: str, started_ms: int, chain: str) -> Optional[dict]:
        h = tx_hash.lower()
        for item in self.cli.tx_history(chain, start_ms=started_ms - 60_000, size=50):
            hashes = [str(item.get("txHash") or "").lower()] + [str(t.get("txHash") or "").lower()
                                                                 for t in item.get("txHashList") or []]
            if h in hashes:
                return item
        return None

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, client_order_id: Optional[str] = None) -> list[dict]:
        """Re-check open intents (one `market-order list` per intent, no waiting). Returns the updated records."""
        updated = []
        for rec in self.intents.open_intents():
            cid = rec["client_order_id"]
            if client_order_id and cid != client_order_id:
                continue
            oid = rec.get("order_id")
            if not oid and rec.get("from_token"):
                try:
                    rows = self.cli.list_orders(from_token=rec["from_token"], to_token=rec.get("to_token"),
                                                start_ms=int(rec.get("created_ms") or 0) - 5000,
                                                chain_id=rec.get("chain_id") or self.chain_id)
                except (BawError, subprocess.TimeoutExpired):
                    continue
                oid = str(rows[0].get("orderId")) if rows else None
                if not oid:
                    continue  # still unknown: stays open until resolve_intent() (user checked the App)
            try:
                rows = self.cli.list_orders(order_id=oid)
            except (BawError, subprocess.TimeoutExpired):
                continue
            row = rows[0] if rows else {}
            st = str(row.get("status") or "").upper()
            new = {"FINISHED": "FINISHED", "FAILED": "FAILED"}.get(st, "PENDING")
            updated.append(self.intents.update(cid, state=new, order_id=oid, tx_hash=row.get("txHash"),
                                               to_qty=row.get("toTokenActualQty")))
        return updated

    def resolve_intent(self, client_order_id: str, state: str = "FAILED", note: str = "resolved manually") -> dict:
        """Close an intent by hand after checking the Binance App (e.g. outcome_unknown with no order)."""
        return self.intents.update(client_order_id, state=state, note=note)

    # ------------------------------------------------------------------ wallet reads (Bot 6 / Bot 1 / Bot 3)
    def address(self) -> Optional[str]:
        return self.cli.address(self.chain_id)

    def get_balances(self) -> dict[str, float]:
        """{symbol: amount} on BSC (display units). Raises BawError/BawSessionExpired."""
        out: dict[str, float] = {}
        for r in self.cli.balances(self.chain_id):
            sym = r.get("symbol")
            try:
                amt = float(r.get("balance") or 0)
            except (TypeError, ValueError):
                continue
            if sym:
                out[sym] = out.get(sym, 0.0) + amt
                if sym == "BNB" and r.get("price"):
                    self._bnb_price = float(r["price"])
        return out

    def get_wallet_settings(self) -> dict:
        """Raw `baw wallet settings --json` data + `fetched_at` (ISO-8601 UTC 'Z'); feeds WalletLimits.from_baw()."""
        raw = self.cli.settings()
        raw["fetched_at"] = utc_now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return raw

    def left_quota(self) -> dict:
        return self.cli.left_quota()

    def get_wallet(self, force: bool = False) -> dict:
        """GET /api/wallet shape (Bot 1). Never raises: session/CLI problems -> connected False + error/warnings."""
        if not force and self._wallet_cache and self._mono() - self._wallet_cache[0] < self.wallet_ttl_s:
            return self._wallet_cache[1]
        w = wallet_snapshot(self.cli, self.chain_id, per_order_cap_usdt=self.max_order_usdt,
                            bnb_gas_reserve=self.bnb_gas_reserve)
        for b in w["balances"]:
            if b["symbol"] == "BNB" and b.get("price"):
                self._bnb_price = b["price"]
        self._wallet_cache = (self._mono(), w)
        return w


def _amounts(item: dict, tx_hash: str, from_token: str, to_token: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(sent from_token, received to_token, gas BNB) from one `tx-history --json` item (display-unit amounts,
    networkFee.feeValue raw with feeTokenDecimals)."""
    sent = recv = gas = None
    ft, tt = from_token.lower(), to_token.lower()
    for t in item.get("txHashList") or []:
        ins = t.get("instructions") or {}
        for s in ins.get("send") or []:
            if str((s.get("tokenInfo") or {}).get("contractAddress", "")).lower() == ft and s.get("amount") not in (None, ""):
                sent = (sent or 0.0) + float(s["amount"])
        for r in ins.get("receive") or []:
            if str((r.get("tokenInfo") or {}).get("contractAddress", "")).lower() == tt and r.get("amount") not in (None, ""):
                recv = (recv or 0.0) + float(r["amount"])
        nf = t.get("networkFee") or {}
        if nf.get("feeValue") not in (None, ""):
            dec = int(nf.get("feeTokenDecimals") or 18)
            gas = (gas or 0.0) + float(nf["feeValue"]) / (10 ** dec)
    return sent, recv, gas


def wallet_snapshot(cli: BawCli, chain_id: str = BSC_CHAIN_ID, per_order_cap_usdt: Optional[float] = None,
                    bnb_gas_reserve: Optional[float] = DEFAULT_BNB_GAS_RESERVE) -> dict:
    """Bot 1's /api/wallet shape from baw reads (status, address, balance, settings). Never raises."""
    now = utc_now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    w: dict[str, Any] = {"provider": "baw", "address": None, "chain": "BSC", "chain_id": int(chain_id),
                         "connected": False, "status": None, "balances": [], "bnb_gas_reserve": bnb_gas_reserve,
                         "limits": {"daily_limit_usdt": None, "daily_used_usdt": None, "daily_left_usdt": None,
                                    "per_order_cap_usdt": per_order_cap_usdt, "allowed_tokens": None,
                                    "trade_all_tokens": None, "quota_date": None, "limit_source": {
                                        "daily_limit_usdt": "wallet", "per_order_cap_usdt": "agent"}},
                         "session_expires_at": None, "abnormal_txn_handling": None, "warnings": [], "error": None,
                         "updated_at": now}
    try:
        st = cli.status()
        w["status"] = st
        if st != "CONNECTED":
            reason = B.SESSION_EXPIRED if st == "UNCONNECTED" else B.WALLET_NOT_READY
            w["error"] = {"reason": reason, "message": f"Agentic Wallet {st}: sign in with `baw auth signin` "
                                                        "and scan the QR in the Binance App"}
            return w
        w["address"] = cli.address(chain_id)
        rows = cli.balances(chain_id)
        bnb = 0.0
        for r in rows:
            amt, val = _f(r.get("balance")), _f(r.get("value"))
            w["balances"].append({"symbol": r.get("symbol"), "amount": amt, "usd_value": val,
                                  "price": _f(r.get("price")), "contract_address": r.get("address")})
            if r.get("symbol") == "BNB":
                bnb += amt or 0.0
        s = cli.settings()
        w["limits"].update(daily_limit_usdt=_f(s.get("dailyLimit")), daily_used_usdt=_f(s.get("quotaUsed")),
                           daily_left_usdt=_f(s.get("quotaLeft")), quota_date=s.get("quotaDate"),
                           trade_all_tokens=s.get("tradeAllTokens"),
                           allowed_tokens=("ALL" if s.get("tradeAllTokens") else "LIMITED")
                           if s.get("tradeAllTokens") is not None else None)
        w["session_expires_at"] = s.get("sessionExpireTime")
        w["abnormal_txn_handling"] = s.get("abnormalTxnHandling")
        w["connected"] = True
        if s.get("abnormalTxnHandling") == "NeedConfirmation":
            w["warnings"].append({"code": B.APP_CONFIRMATION_REQUIRED, "message": "Wallet asks for App confirmation on "
                                  "abnormal trades; set it to AutoReject for autonomous trading"})
        if bnb_gas_reserve is not None and bnb < bnb_gas_reserve:
            w["warnings"].append({"code": B.INSUFFICIENT_GAS, "message": f"BNB {bnb:.6f} < gas reserve {bnb_gas_reserve}"})
        if s.get("tradeAllTokens") is False:
            w["warnings"].append({"code": B.POLICY_REJECTED, "message": "Wallet token scope is Limited; the allowed "
                                  "list is not readable, rejected tokens show up as policy_rejected"})
    except BawError as e:
        w["error"] = {"reason": e.reason, "message": e.args[0]}
        w["connected"] = False if e.reason in B.PAUSE_REASONS else w["connected"]
    except subprocess.TimeoutExpired:
        w["error"] = {"reason": B.BAW_TIMEOUT, "message": "baw read timed out"}
    return w


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def default_counter_token(chain_id: str = BSC_CHAIN_ID) -> str:
    return QUOTE_ASSETS[chain_id].address


def new_client_order_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- LIVE client
from .live import BinanceWeb3Client  # noqa: E402
from .wallet import LiveWalletMixin  # noqa: E402


class BawLiveClient(LiveWalletMixin, BinanceWeb3Client):
    """LIVE ExchangeClient: market data, token status and quotes (with price_impact_pct via quote_meta /
    quote_or_reason) from the Binance Web3 API client; execution and wallet reads through `baw` (BawExecutor).
    Adds get_balances() / get_wallet() / get_wallet_settings()."""

    executor: BawExecutor

"""Binance Agentic Wallet CLI (`baw`, npm @binance/agentic-wallet 1.10.0) - subprocess wrapper.

Everything here is derived from `baw <cmd> --help` and the CLI's bundled source (dist/index.js, v1.10.0):
- every command takes `--json`; success prints `{"success": true, "data": ...}` to STDOUT (exit 0);
- an error prints `{"success": false, "error": {"code", "name", "message", "data"?}}` to STDOUT and exits 1;
- the CLI has no HTTP timeout of its own, so every call here has one (20 s reads, 45 s swap submit).

Read commands only, plus `market-order swap` (used solely by BawExecutor behind the LIVE gate). `read_only=True`
(used by baw_smoke.py) refuses every command that is not in READ_COMMANDS before anything is spawned.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .errors import ExchangeError

BSC_CHAIN_ID = "56"
READ_TIMEOUT_S = 20.0
SWAP_TIMEOUT_S = 45.0
PACKAGE_ENTRY = ("node_modules", "@binance", "agentic-wallet", "dist", "index.js")  # package.json "bin": dist/index.js

# --------------------------------------------------------------------------- reason codes (stable strings)
SESSION_EXPIRED = "session_expired"            # not signed in / session expired -> pause loop + alert (re-scan QR)
WALLET_NOT_READY = "wallet_not_ready"          # wallet missing / still being created
LIVE_DISABLED = "live_disabled"                # LIVE_TRADING_ENABLED / BINANCE_LIVE_ENABLED / RISK_ALLOW_LIVE not all true
ORDER_CAP_EXCEEDED = "order_cap_exceeded"      # notional > BINANCE_LIVE_MAX_ORDER_USDT (buys) / sell cap
TX_LOCKED = "tx_locked"                        # `wallet tx-lock` LOCKED (a tx or App confirmation in progress)
ORDER_IN_FLIGHT = "order_in_flight"            # another swap in this process, a PENDING market order, or an unresolved intent
APP_CONFIRMATION_REQUIRED = "app_confirmation_required"  # abnormalTxnHandling=NeedConfirmation: tap in Binance App
ABNORMAL_TXN_REJECTED = "abnormal_txn_rejected"          # wallet risk control (AutoReject) refused the trade
QUOTA_EXCEEDED = "quota_exceeded"              # wallet daily limit (dailyLimit/quotaLeft) would be exceeded
POLICY_REJECTED = "policy_rejected"            # token not allowed (Limited scope) / compliance
INSUFFICIENT_BALANCE = "insufficient_balance"
INSUFFICIENT_GAS = "insufficient_gas"
SLIPPAGE_EXCEEDED = "slippage_exceeded"        # slippage / min-receive revert or rejection
NO_ROUTE = "no_route"                          # no route / no liquidity
MARKET_CLOSED = "market_closed"
AMOUNT_OUT_OF_RANGE = "amount_out_of_range"    # below minimum / above max single order
QUOTE_EXPIRED = "quote_expired"
SERVICE_UNAVAILABLE = "service_unavailable"    # network / 429 / 503 - back off
BAW_TIMEOUT = "baw_timeout"                    # our subprocess timeout fired
BAW_NOT_FOUND = "baw_not_found"
OUTCOME_UNKNOWN = "outcome_unknown"            # swap may or may not exist; intent kept open, call reconcile()
PENDING_CONFIRMATION = "pending_confirmation"  # order exists, still PENDING after the poll window
ORDER_FAILED = "order_failed"                  # order FAILED on chain / broadcast failed
ORDER_REJECTED = "order_rejected"              # other server rejection
BAW_ERROR = "baw_error"                        # anything else

PAUSE_REASONS = {SESSION_EXPIRED, WALLET_NOT_READY}  # the loop should pause LIVE and alert the user

# CLI error codes (dist/index.js enum `d`) -> reason
_CODE_REASONS: dict[int, str] = {
    10003000: SESSION_EXPIRED, 10003001: SESSION_EXPIRED, 10003002: SESSION_EXPIRED, 10003003: SESSION_EXPIRED,
    10003004: SESSION_EXPIRED, 100001005: SESSION_EXPIRED,
    20001001: WALLET_NOT_READY, 20001002: WALLET_NOT_READY,
    20002002: INSUFFICIENT_BALANCE, 20002003: INSUFFICIENT_GAS,
    30001001: AMOUNT_OUT_OF_RANGE, 30001002: POLICY_REJECTED, 30001003: POLICY_REJECTED, 351765: POLICY_REJECTED,
    30001004: APP_CONFIRMATION_REQUIRED,
    30002002: QUOTE_EXPIRED, 30002003: AMOUNT_OUT_OF_RANGE, 30002004: NO_ROUTE,
    30003002: SLIPPAGE_EXCEEDED, 30003003: ORDER_FAILED, 30003004: ORDER_FAILED, 30003005: ORDER_FAILED,
    30003006: OUTCOME_UNKNOWN,
    30006002: TX_LOCKED,
    50001001: SERVICE_UNAVAILABLE, 50001002: SERVICE_UNAVAILABLE, 50001003: SERVICE_UNAVAILABLE,
    50001004: SERVICE_UNAVAILABLE, 50001005: SERVICE_UNAVAILABLE,
    # Binance Web3 trading codes that baw may pass through as service errors
    40366: AMOUNT_OUT_OF_RANGE, 40375: AMOUNT_OUT_OF_RANGE, 40367: MARKET_CLOSED, 40369: MARKET_CLOSED,
    40374: NO_ROUTE, 40370: POLICY_REJECTED, 40368: POLICY_REJECTED,
}
# Codes after which a swap MAY have been placed: never retry, reconcile instead.
AMBIGUOUS_CODES = {30003006, 50001001, 50001002, 50001003}

# ORDER_API_ERROR / QUOTE_API_ERROR / service errors pass the server message through: classify by keywords.
_MESSAGE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SLIPPAGE_EXCEEDED, ("slippage", "min receive", "minreceive", "min_receive", "minimum receive", "insufficient_output",
                         "insufficient output", "too little received", "return amount is not enough", "price moved")),
    (QUOTA_EXCEEDED, ("daily limit", "dailylimit", "quota", "trading limit", "exceed limit", "exceeds limit",
                      "limit exceeded", "exceeded the limit")),
    (APP_CONFIRMATION_REQUIRED, ("confirm on app", "confirmation required", "confirm in the app", "app confirmation")),
    (ABNORMAL_TXN_REJECTED, ("abnormal", "risky", "high risk", "high-risk", "risk control", "price impact")),
    (POLICY_REJECTED, ("not allowed", "not supported", "whitelist", "white list", "compliance", "restricted",
                       "cannot be traded", "not tradable", "unsupported token", "token scope", "verified token")),
    (MARKET_CLOSED, ("market closed", "trading time", "market state", "not open", "offhours")),
    (NO_ROUTE, ("no route", "liquidity")),
    (INSUFFICIENT_GAS, ("insufficient gas", "gas fee")),
    (INSUFFICIENT_BALANCE, ("insufficient balance", "insufficient funds", "exceeds wallet balance")),
)


_PASS_THROUGH = {30003001, 30002001, 40001001, 99999999}  # ORDER_API_ERROR / QUOTE_API_ERROR / LIMIT_ORDER_API_ERROR


def classify(code: Optional[int], message: str = "", name: str = "") -> str:
    """baw error -> reason code. Well-known CLI codes map directly; pass-through server errors (ORDER_API_ERROR,
    QUOTE_API_ERROR, unknown/service codes) are classified by message keywords."""
    if code is not None and code in _CODE_REASONS and code not in _PASS_THROUGH:
        return _CODE_REASONS[code]
    text = f"{name} {message}".lower()
    for reason, words in _MESSAGE_RULES:
        if any(w in text for w in words):
            return reason
    if code in (30003001, 40001001):
        return ORDER_REJECTED
    if code == 30002001:
        return NO_ROUTE
    return BAW_ERROR


class BawError(ExchangeError):
    """A `baw` failure with a stable `reason` (see the reason-code constants above)."""

    def __init__(self, message: str, code: Optional[int] = None, name: str = "", reason: Optional[str] = None,
                 data: Optional[dict] = None, ambiguous: bool = False) -> None:
        super().__init__(message, code)
        self.name = name
        self.reason = reason or classify(code, message, name)
        self.data = data or {}
        self.ambiguous = ambiguous or (code in AMBIGUOUS_CODES)

    @property
    def order_id(self) -> Optional[str]:
        v = self.data.get("orderId")
        return str(v) if v not in (None, "") else None

    def __str__(self) -> str:  # "[reason] message"
        return f"[{self.reason}] {self.args[0]}"


class BawSessionExpired(BawError):
    """Not signed in / session expired. Re-sign-in needed: `baw auth signin` + scan the QR in the Binance App."""


# --------------------------------------------------------------------------- command resolution + runner
@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[Sequence[str], float], RunResult]  # (argv, timeout_s) -> RunResult; may raise TimeoutExpired


def _npm_global_roots(env: dict) -> list[Path]:
    roots = []
    if env.get("APPDATA"):
        roots.append(Path(env["APPDATA"]) / "npm")  # default Windows npm prefix
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        try:
            out = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=10, check=False,
                                 shell=False).stdout.strip()
            if out:
                roots.append(Path(out).parent)  # `npm root -g` = <prefix>/node_modules
        except (OSError, subprocess.SubprocessError):
            pass
    return roots


def resolve_baw_command(env: Optional[dict] = None, platform: Optional[str] = None) -> list[str]:
    """argv prefix for baw.

    1. BAW_CMD: a JSON list (`["node", "C:\\...\\index.js"]`), a path to dist/index.js (run with node), or a binary path.
    2. Windows: `node <npm prefix>\\node_modules\\@binance\\agentic-wallet\\dist\\index.js` (avoids baw.cmd + cmd.exe quoting).
    3. Otherwise `baw` from PATH.
    """
    env = dict(os.environ if env is None else env)
    platform = platform or sys.platform
    raw = (env.get("BAW_CMD") or "").strip()
    if raw:
        if raw.startswith("["):
            return [str(x) for x in json.loads(raw)]
        if raw.lower().endswith(".js"):
            return [env.get("NODE_CMD") or shutil.which("node") or "node", raw]
        return [raw]
    if platform.startswith("win"):
        for root in _npm_global_roots(env):
            entry = root.joinpath(*PACKAGE_ENTRY)
            if entry.is_file():
                return [env.get("NODE_CMD") or shutil.which("node") or "node", str(entry)]
        found = shutil.which("baw.cmd") or shutil.which("baw")
        return [found] if found else ["baw"]
    return [shutil.which("baw") or "baw"]


def subprocess_runner() -> Runner:
    def run(argv: Sequence[str], timeout_s: float) -> RunResult:
        env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
        kw: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, check=False, env=env, **kw)
        return RunResult(p.returncode, p.stdout or "", p.stderr or "")

    return run


def _parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        i = text.find("{")  # tolerate stray lines before the JSON document
        if i > 0:
            try:
                return json.loads(text[i:])
            except ValueError:
                return None
    return None


def fmt_qty(x: float | str | Decimal, places: int = 18) -> str:
    """Plain decimal (never scientific - baw validates /^\\d+(\\.\\d+)?$/-style), rounded DOWN to `places`."""
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(d.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".") or "0"


def fmt_slippage(pct: float) -> str:
    if not 0 <= float(pct) <= 100:
        raise ValueError("slippage percent must be within 0-100")
    return fmt_qty(pct, 4)


READ_COMMANDS = {
    ("wallet", "status"), ("wallet", "address"), ("wallet", "balance"), ("wallet", "settings"),
    ("wallet", "left-quota"), ("wallet", "tx-lock"), ("wallet", "tx-history"), ("wallet", "chains"),
    ("wallet", "gas-price"), ("market-order", "quote"), ("market-order", "list"), ("cli-check",),
}


class BawCli:
    """Typed, timeout-bounded `baw ... --json` calls. Raises BawError (or BawSessionExpired) with a reason."""

    def __init__(self, runner: Optional[Runner] = None, cmd: Optional[Sequence[str]] = None,
                 read_timeout_s: float = READ_TIMEOUT_S, swap_timeout_s: float = SWAP_TIMEOUT_S,
                 read_only: bool = False, allowed: Optional[set[tuple[str, ...]]] = None) -> None:
        self.runner = runner or subprocess_runner()
        self.allowed = set(allowed) if allowed is not None else None  # allow-list of (group, cmd); None = any
        self._cmd = list(cmd) if cmd else None
        self.read_timeout_s, self.swap_timeout_s = read_timeout_s, swap_timeout_s
        self.read_only = read_only

    @property
    def cmd(self) -> list[str]:
        if self._cmd is None:
            self._cmd = resolve_baw_command()
        return self._cmd

    def call(self, *args: str, timeout_s: Optional[float] = None) -> Any:
        """Run `baw <args> --json` and return `data`. subprocess.TimeoutExpired propagates (callers decide)."""
        key = tuple(a for a in args[:2] if not a.startswith("-"))
        if self.read_only and key not in READ_COMMANDS and key[:1] not in READ_COMMANDS:
            raise BawError(f"read-only baw client refuses `{' '.join(key)}`", reason=BAW_ERROR)
        if self.allowed is not None and key not in self.allowed:
            raise BawError(f"this baw client is limited to {sorted(' '.join(k) for k in self.allowed)}; "
                           f"refused `{' '.join(key)}`", reason=BAW_ERROR)
        argv = [*self.cmd, *args, "--json"]
        try:
            res = self.runner(argv, timeout_s or self.read_timeout_s)
        except FileNotFoundError as e:
            raise BawError(f"baw not found ({e}); npm i -g @binance/agentic-wallet@1.10.0 or set BAW_CMD",
                           reason=BAW_NOT_FOUND) from e
        payload = _parse_json(res.stdout)
        if not isinstance(payload, dict) or "success" not in payload:
            payload = _parse_json(res.stderr)
        if not isinstance(payload, dict) or "success" not in payload:
            out = (res.stdout or res.stderr or "").strip()[:300]
            raise BawError(f"baw returned non-JSON output (exit {res.returncode}): {out}", reason=BAW_ERROR)
        if payload.get("success") is not True:
            err = payload.get("error") or {}
            code = err.get("code")
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            msg = str(err.get("message") or "baw error")
            name = str(err.get("name") or "")
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            reason = classify(code, msg, name)
            cls = BawSessionExpired if reason == SESSION_EXPIRED else BawError
            raise cls(f"baw {' '.join(key)}: {name or code} {msg}".strip(), code, name, reason, data)
        return payload.get("data")

    # ------------------------------------------------------------------ reads
    def status(self) -> str:
        """UNCONNECTED | CREATING | CONNECTED (no session -> UNCONNECTED, exit 0).

        Observed twice on baw 1.10.0 (11 Oct 2026, Windows): when the session silently dies, `wallet status` itself fails
        with SERVICE_ERROR code 2 "illegal parameter" (and every other call fails the same way) until `baw auth signin`.
        `wallet status` takes no parameters, so on this command it means "no valid session": raise BawSessionExpired so
        the app pauses LIVE and shows Reconnect. Scoped to `wallet status` only, so a real bad parameter on a quote or
        swap keeps its own reason."""
        try:
            data = self.call("wallet", "status")
        except BawSessionExpired:
            raise
        except BawError as e:
            if "illegal parameter" in str(e).lower():
                raise BawSessionExpired(f"baw wallet status: {e.args[0]} (treated as an expired session: sign in again)",
                                        e.code, e.name, SESSION_EXPIRED, e.data) from e
            raise
        return str((data or {}).get("status") or "UNKNOWN")

    def addresses(self) -> list[dict]:
        return list((self.call("wallet", "address") or {}).get("addresses") or [])

    def address(self, chain_id: str = BSC_CHAIN_ID) -> Optional[str]:
        rows = self.addresses()
        for r in rows:
            if str(r.get("binanceChainId")) == str(chain_id):
                return r.get("address")
        evm = [r for r in rows if str(r.get("address", "")).startswith("0x")]
        return evm[0]["address"] if evm else None

    def balances(self, chain_id: str = BSC_CHAIN_ID) -> list[dict]:
        """[{symbol, address, binanceChainId, balance, price, value}] (display units; < $0.01 hidden by baw)."""
        return list(self.call("wallet", "balance", "--binanceChainId", str(chain_id)) or [])

    def settings(self) -> dict:
        return dict(self.call("wallet", "settings") or {})

    def left_quota(self) -> dict:
        """{quotaUsed, quotaLeft, dailyLimit, date} (USD, UTC date)."""
        return dict(self.call("wallet", "left-quota") or {})

    def tx_lock(self, chain_id: str = BSC_CHAIN_ID) -> str:
        return str((self.call("wallet", "tx-lock", "--binanceChainId", str(chain_id)) or {}).get("status") or "UNKNOWN")

    def tx_status(self, tx_hash: str, chain_id: str = BSC_CHAIN_ID) -> Optional[dict]:
        """`tx-history --tx`: {binanceChainId, txHash, status: PENDING|SUCCESS|FAILED, txTime} or None (not found)."""
        return self.call("wallet", "tx-history", "--tx", tx_hash, "--binanceChainId", str(chain_id))

    def tx_history(self, chain_id: str = BSC_CHAIN_ID, start_ms: Optional[int] = None, size: int = 20,
                   tx_type: str = "all") -> list[dict]:
        args = ["wallet", "tx-history", "--type", tx_type, "--binanceChainId", str(chain_id), "--size", str(size)]
        if start_ms:
            args += ["--startTime", str(int(start_ms))]
        return list((self.call(*args) or {}).get("transactions") or [])

    def quote(self, from_token: str, to_token: str, from_qty: float | str, slippage_pct: float,
              chain_id: str = BSC_CHAIN_ID) -> dict:
        """{fromCoinSymbol, fromCoinAmount, toCoinSymbol, toCoinAmount, slippage(fraction)}."""
        return dict(self.call("market-order", "quote", "--binanceChainId", str(chain_id),
                              "--fromTokenQty", fmt_qty(from_qty), "--fromToken", from_token, "--toToken", to_token,
                              "--slippage", fmt_slippage(slippage_pct)) or {})

    def list_orders(self, order_id: Optional[str] = None, status: Optional[str] = None,
                    from_token: Optional[str] = None, to_token: Optional[str] = None,
                    start_ms: Optional[int] = None, chain_id: Optional[str] = BSC_CHAIN_ID,
                    page_size: int = 20) -> list[dict]:
        """Rows {orderId, chain, fromToken, fromTokenQty, toToken, toTokenActualQty, status, slippage, txHash, ...}."""
        if order_id:
            args = ["market-order", "list", "--orderId", str(order_id)]
        else:
            args = ["market-order", "list", "--pageSize", str(page_size)]
            if chain_id:
                args += ["--binanceChainId", str(chain_id)]
            if status:
                args += ["--status", status]
            if from_token:
                args += ["--fromToken", from_token]
            if to_token:
                args += ["--toToken", to_token]
            if start_ms:
                args += ["--startTime", str(int(start_ms))]
        return list((self.call(*args) or {}).get("list") or [])

    # ------------------------------------------------------------------ x402 (B402) pay-per-call
    def x402_preview(self, requirements: str | dict) -> dict:
        """`baw x402-payment preview --paymentRequirements <value> --json`. No spend.

        `requirements` is the 402's base64 `PAYMENT-REQUIRED` header value (passed as is) or the decoded PaymentRequired
        dict (sent base64-encoded: baw auto-detects base64 vs JSON, and base64 avoids Windows quoting problems).
        Returns {paymentId, options: [{index, status: READY_TO_SIGN|ACTION_REQUIRED|NOT_SIGNABLE, reasons[], scheme,
        assetTransferMethod, binanceChainId, amount (display units), tokenSymbol, tokenAddress, amountUsd, payTo,
        currentBalance, currentBalanceUsd, userWalletAddress, needApproveFirst}]} (dist/index.js 1.10.0)."""
        if isinstance(requirements, dict):
            requirements = base64.b64encode(json.dumps(requirements, separators=(",", ":")).encode()).decode()
        return dict(self.call("x402-payment", "preview", "--paymentRequirements", str(requirements)) or {})

    def x402_sign(self, payment_id: str, selected_index: int) -> dict:
        """`baw x402-payment sign --paymentId <id> --selectedIndex <n> --json`. SPENDS (authorises the payment).

        Returns {paymentHeaderName, paymentHeaderValue, signatureExpiresAt (unix s), approveTxHash?, binanceChainId?};
        approveTxHash is set when baw had to dispatch a Permit2 approve tx first."""
        return dict(self.call("x402-payment", "sign", "--paymentId", str(payment_id),
                              "--selectedIndex", str(int(selected_index)), timeout_s=self.swap_timeout_s) or {})

    # ------------------------------------------------------------------ the one write
    def swap(self, from_token: str, to_token: str, from_qty: float | str, slippage_pct: float,
             chain_id: str = BSC_CHAIN_ID, mev: bool = True, gas_level: str = "MEDIUM") -> dict:
        """SUBMIT a market order -> {orderId, clientOrderId?}. Only a submission: poll list_orders(order_id)."""
        return dict(self.call("market-order", "swap", "--binanceChainId", str(chain_id),
                              "--fromTokenQty", fmt_qty(from_qty), "--fromToken", from_token, "--toToken", to_token,
                              "--slippage", fmt_slippage(slippage_pct), "--mev", "true" if mev else "false",
                              "--gasLevel", gas_level, timeout_s=self.swap_timeout_s) or {})

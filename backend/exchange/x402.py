"""Paid "second opinion" from BNB Chain's Stock Analyze Agent, paid per call over x402 with the Agentic Wallet (`baw`).

Service: https://stock-agent.bnbchain.org (shapes verified read-only on 2026-10-11, samples in tests/samples/):
- `GET /x402/price` -> 200 {x402Version: 2, paymentRequired, price_u: "0.1", price_wei, asset, network: "eip155:56",
  payTo, accepts: [...], supportedAssets: [{symbol: U|USD1|USDC|USDT, asset, decimals: 18, transferMethod}], ...}
- `POST /x402/analyze/async` {"symbols": ["NVDA"], "analysis_type": "comprehensive"} without payment -> 402,
  header `payment-required: <base64 PaymentRequired>`, body {error, description, paymentRequired: {x402Version: 2,
  accepts: [{scheme: exact, network, amount (wei), asset, payTo, maxTimeoutSeconds, extra: {name, version,
  assetTransferMethod: eip3009|permit2-exact, ...}}], error, resource}}.
- Replay with `PAYMENT-SIGNATURE: <baw paymentHeaderValue>` -> 202 + `payment-response` header + {jobId, jobToken}
  (campaign docs; NOT exercised here, it spends). 503 settlement_pending -> replay the SAME proof, never re-sign.
  429 wallet_rate_limited (30 new calls / wallet / rolling hour) -> stop, honour Retry-After.
- `GET /x402/jobs/{jobId}` + `X-Job-Token` -> status queued|running|succeeded|failed; succeeded has `downloadUrl`
  (S3 presigned, Markdown report). failed + retryable -> `POST /x402/jobs/{jobId}/resume` (no new charge).

Payment comes from the wallet's own x402 quota (`x402DailyLimit` $20/day, separate from the trading `dailyLimit`;
`baw wallet settings` reads them from separate fields). This module never trades: its baw client is allow-listed to
`wallet status`, `wallet settings` and `x402-payment preview|sign`.

Gates, all checked BEFORE `x402-payment sign` (the only step that spends):
STOCK_AGENT_X402_ENABLED (default false) -> session CONNECTED -> caller guard (emergency stop / different wallet) ->
daily call cap (STOCK_AGENT_X402_DAILY_CALLS, default 5, UTC day, local JSON in STATE_DIR) -> option READY_TO_SIGN,
no Permit2 approve, payTo == pinned payee -> amount <= STOCK_AGENT_X402_MAX_PRICE (default 0.1) -> x402 quota left ->
token balance (USDT keeps STOCK_AGENT_X402_USDT_RESERVE for exits/fees).
jobId + jobToken are written to the state file the moment the replay returns (they cannot be recovered).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .baw import BawCli, BawError, BawSessionExpired, SESSION_EXPIRED
from .errors import ExchangeError
from .tokens import DEFAULT_TOKENS

log = logging.getLogger("backend.exchange.x402")

BASE_URL = "https://stock-agent.bnbchain.org"
# payTo observed in GET /x402/price and the 402 on 2026-10-11. A 402 asking to pay anyone else is refused.
PINNED_PAY_TO = "0xd10bddc20e4dc42a1a19a9653e994991e25b8153"
TOKEN_PREFERENCE = ("U", "USD1", "USDC", "USDT")  # U/USD1 are EIP-3009 (no approve); campaign docs say prefer them
PAYMENT_HEADER = "PAYMENT-SIGNATURE"
STATE_FILE = "x402_state.json"
ALLOWED_BAW = {("wallet", "status"), ("wallet", "settings"), ("x402-payment", "preview"), ("x402-payment", "sign")}

# reason codes (stable strings for the API/UI)
X402_DISABLED = "x402_disabled"
SESSION = SESSION_EXPIRED                      # "session_expired"
GUARD_BLOCKED = "guard_blocked"                # emergency stop / different-wallet block / caller guard
DAILY_CAP = "daily_cap_reached"
PRICE_OVER_CAP = "price_over_cap"
QUOTA_EXCEEDED = "x402_quota_exceeded"
EXIT_RESERVE = "exit_reserve"                  # paying in USDT would eat the reserve kept for exits/fees
INSUFFICIENT_BALANCE = "insufficient_balance"
NO_SIGNABLE_OPTION = "no_signable_option"
APPROVE_REQUIRED = "approve_required"          # only Permit2 options needing an on-chain approve: refused
PAY_TO_MISMATCH = "pay_to_mismatch"
UNEXPECTED_RESPONSE = "unexpected_response"
RATE_LIMITED = "rate_limited"
PAYMENT_FAILED = "payment_failed"
SERVICE_UNAVAILABLE = "service_unavailable"
JOB_NOT_FOUND = "job_not_found"
STATE_WRITE_FAILED = "state_write_failed"
BAD_SYMBOL = "bad_symbol"


class X402Error(ExchangeError):
    def __init__(self, reason: str, message: str, data: Optional[dict] = None, http_status: Optional[int] = None):
        super().__init__(message)
        self.reason, self.data, self.http_status = reason, data or {}, http_status

    def __str__(self) -> str:
        return f"[{self.reason}] {self.args[0]}"

    def to_dict(self) -> dict:
        return {"reason": self.reason, "message": self.args[0], **({"data": self.data} if self.data else {})}


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_UNDERLYING = {t.symbol.upper(): t.underlying for t in DEFAULT_TOKENS if t.underlying}


def underlying_ticker(symbol: str) -> str:
    """App symbol -> plain ticker the analyst expects: NVDAB/NVDAon -> NVDA, TSLAx -> TSLA, NVDA -> NVDA."""
    s = (symbol or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,14}", s):
        raise X402Error(BAD_SYMBOL, f"not a stock symbol: {symbol!r}")
    if s.upper() in _UNDERLYING:
        return str(_UNDERLYING[s.upper()]).upper()
    for suf in ("on", "x"):
        if s.endswith(suf) and len(s) > len(suf) and s[:-len(suf)].isupper():
            return s[:-len(suf)]
    if s.endswith("B") and len(s) > 2 and s.isupper() and s[:-1] in {u for u in _UNDERLYING.values()}:
        return s[:-1]
    return s.upper()


def _b64json(value: str) -> Optional[dict]:
    try:
        d = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)).decode())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


Guard = Callable[[dict], Optional[str]]  # context -> refusal message (refuse) or None (go on)


class StockAgentX402:
    def __init__(self, baw: Optional[BawCli] = None, http: Optional[httpx.Client] = None,
                 base_url: Optional[str] = None, state_dir: Optional[str] = None, env: Optional[dict] = None,
                 guard: Optional[Guard] = None, on_paid: Optional[Callable[[dict], None]] = None,
                 sleep: Callable[[float], None] = time.sleep, price_ttl_s: float = 60.0) -> None:
        e = dict(os.environ if env is None else env)
        self.enabled = _truthy(e.get("STOCK_AGENT_X402_ENABLED"))
        self.max_price = float(e.get("STOCK_AGENT_X402_MAX_PRICE") or 0.1)
        self.daily_calls = int(e.get("STOCK_AGENT_X402_DAILY_CALLS") or 5)
        self.usdt_reserve = float(e.get("STOCK_AGENT_X402_USDT_RESERVE") or 0)
        self.pay_to = (e.get("STOCK_AGENT_X402_PAY_TO") or PINNED_PAY_TO).lower()
        self.base_url = (base_url or e.get("STOCK_AGENT_X402_URL") or BASE_URL).rstrip("/")
        sd = state_dir or e.get("STATE_DIR") or e.get("LOG_DIR") or str(Path.home() / ".teamwork")
        self.state_path = Path(sd) / STATE_FILE
        if baw is None:
            baw = BawCli(allowed=ALLOWED_BAW)
        elif baw.allowed is None:
            baw.allowed = set(ALLOWED_BAW)  # never trade, even with a caller-supplied client
        self.baw = baw
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0),
                                         headers={"User-Agent": "teamwork-regime/x402", "Accept": "application/json"})
        self.guard, self.on_paid, self._sleep = guard, on_paid, sleep
        self.price_ttl_s = price_ttl_s
        self._price_cache: Optional[tuple[float, dict]] = None
        self._lock = threading.RLock()
        self._analyze_lock = threading.Lock()
        self.limits_dirty = False  # True after a paid call: the app should re-read wallet limits (then clear it)

    # ------------------------------------------------------------------ state (atomic JSON)
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            log.exception("x402 state %s unreadable", self.state_path)
            raise X402Error(STATE_WRITE_FAILED, f"x402 state file {self.state_path} is unreadable; fix or move it "
                            "before paying (it holds paid job tokens)")

    def _save(self, d: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), prefix=".x402.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def calls_today(self) -> int:
        with self._lock:
            return int((self._load().get("calls") or {}).get(_utc_day(), 0))

    def jobs(self) -> dict[str, dict]:
        """Saved jobs without their tokens: {jobId: {symbol, paid_usdt, tx_hash, status, created_at, ...}}."""
        with self._lock:
            return {k: {kk: vv for kk, vv in v.items() if kk != "job_token"}
                    for k, v in (self._load().get("jobs") or {}).items()}

    # ------------------------------------------------------------------ reads
    def _price_raw(self) -> dict:
        now = time.monotonic()
        if self._price_cache and now - self._price_cache[0] < self.price_ttl_s:
            return self._price_cache[1]
        try:
            r = self.http.get(f"{self.base_url}/x402/price")
        except httpx.HTTPError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"GET /x402/price failed: {e}") from e
        if r.status_code != 200:
            raise X402Error(SERVICE_UNAVAILABLE, f"GET /x402/price -> HTTP {r.status_code}", http_status=r.status_code)
        d = r.json()
        self._price_cache = (now, d)
        return d

    def price_usdt(self) -> Optional[float]:
        d = self._price_raw()
        p = _num(d.get("price_u"))
        if p is None:
            amounts = [_num(a.get("amount")) for a in d.get("accepts") or []]
            amounts = [a for a in amounts if a is not None]
            p = min(amounts) / 1e18 if amounts else None
        return p

    def quota(self) -> dict:
        """x402 quota from `baw wallet settings` (USD): {daily_limit, used, left, date}. Separate from trading dailyLimit."""
        try:
            s = self.baw.settings()
        except BawSessionExpired as e:
            raise X402Error(SESSION, f"wallet signed out: {e.args[0]}") from e
        except BawError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"baw wallet settings failed: {e}") from e
        return {"daily_limit": _num(s.get("x402DailyLimit")), "used": _num(s.get("x402QuotaUsed")),
                "left": _num(s.get("x402QuotaLeft")), "date": s.get("x402QuotaDate")}

    def price(self, symbol: Optional[str] = None) -> dict:
        """Bot 1's `GET /api/x402/price?symbol=` shape:
        {enabled, price_usdt, cap_usdt, calls_left_today, quota_usdt_left|null}. The service price is flat (symbol unused).
        Never raises for a signed-out wallet or a slow service: those fields come back null."""
        try:
            p = self.price_usdt()
        except X402Error:
            p = None
        try:
            q = self.quota()["left"] if self.enabled else None
        except X402Error:
            q = None
        return {"enabled": self.enabled, "price_usdt": p, "cap_usdt": self.max_price,
                "calls_left_today": max(0, self.daily_calls - self.calls_today()), "quota_usdt_left": q}

    def status(self) -> dict:
        """Superset for the README's /api/x402/status: price() + daily cap numbers."""
        d = self.price()
        d.update(cap=self.daily_calls, calls_today=self.calls_today(), limits_dirty=self.limits_dirty)
        return d

    # ------------------------------------------------------------------ the paid call
    def _choose(self, preview: dict, req_amounts: dict[str, float]) -> dict:
        opts = list(preview.get("options") or [])
        ready = [o for o in opts if o.get("status") == "READY_TO_SIGN"]
        if not ready:
            why = "; ".join(f"[{o.get('index')}] {o.get('tokenSymbol')} {o.get('status')}: "
                            f"{','.join(o.get('reasons') or []) or '-'}" for o in opts) or "no options"
            raise X402Error(NO_SIGNABLE_OPTION, f"no READY_TO_SIGN payment option ({why})",
                            {"options": opts})
        no_approve = [o for o in ready if not o.get("needApproveFirst")]
        if not no_approve:
            raise X402Error(APPROVE_REQUIRED, "every payable option needs a Permit2 approve transaction first; refused "
                            "(fund the wallet with U or USD1 instead)", {"options": ready})
        paying = [o for o in no_approve if str(o.get("payTo") or "").lower() == self.pay_to]
        if not paying:
            raise X402Error(PAY_TO_MISMATCH, f"payment requested to {no_approve[0].get('payTo')}, expected {self.pay_to}")

        def rank(o: dict) -> tuple:
            sym = str(o.get("tokenSymbol") or "").upper()
            return (TOKEN_PREFERENCE.index(sym) if sym in TOKEN_PREFERENCE else 99, int(o.get("index") or 0))
        paying.sort(key=rank)
        # skip USDT if a cheaper-on-exits token is payable; balance checks happen after the choice
        for o in paying:
            bal, amt = _num(o.get("currentBalance")), self._amount(o, req_amounts)
            if bal is None or amt is None or bal >= amt + (self.usdt_reserve if self._is_usdt(o) else 0):
                return o
        return paying[0]

    @staticmethod
    def _is_usdt(o: dict) -> bool:
        return str(o.get("tokenSymbol") or "").upper() == "USDT"

    @staticmethod
    def _amount(o: dict, req_amounts: dict[str, float]) -> Optional[float]:
        """Highest of the option's display amount, its USD value and the 402's own wei amount for that asset."""
        vals = [_num(o.get("amount")), _num(o.get("amountUsd")),
                req_amounts.get(str(o.get("tokenAddress") or "").lower())]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def _post_analyze(self, body: dict, headers: Optional[dict] = None) -> httpx.Response:
        try:
            return self.http.post(f"{self.base_url}/x402/analyze/async", json=body,
                                  headers={"Content-Type": "application/json", **(headers or {})})
        except httpx.HTTPError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"POST /x402/analyze/async failed: {e}") from e

    def analyze(self, symbol: str, *, analysis_type: str = "comprehensive", guard: Optional[Guard] = None,
                emergency_stop: bool = False, wallet_blocked: bool = False,
                usdt_reserve: Optional[float] = None) -> dict:
        """Pay for one analysis and return Bot 1's shape {job_id, paid_usdt, tx_hash|null}. Never trades.

        Refuses before signing (X402Error.reason) when: disabled, session expired, emergency_stop / wallet_blocked /
        guard(ctx) returns a message, daily call cap hit, price > cap, x402 quota short, or the payment would leave
        USDT below `usdt_reserve` (default STOCK_AGENT_X402_USDT_RESERVE). One paid call at a time per process."""
        if not self.enabled:
            raise X402Error(X402_DISABLED, "paid second opinion is off (set STOCK_AGENT_X402_ENABLED=true)")
        ticker = underlying_ticker(symbol)
        reserve = self.usdt_reserve if usdt_reserve is None else float(usdt_reserve)
        if not self._analyze_lock.acquire(blocking=False):
            raise X402Error(GUARD_BLOCKED, "another paid analysis is being submitted; wait for it")
        try:
            return self._analyze(ticker, analysis_type, guard, emergency_stop, wallet_blocked, reserve)
        finally:
            self._analyze_lock.release()

    def _refuse_if_guarded(self, ctx: dict, guard: Optional[Guard], emergency_stop: bool, wallet_blocked: bool) -> None:
        if emergency_stop:
            raise X402Error(GUARD_BLOCKED, "emergency stop is active; no paid calls")
        if wallet_blocked:
            raise X402Error(GUARD_BLOCKED, "the signed-in wallet differs from the agent's wallet; no paid calls")
        for g in (self.guard, guard):
            if g is not None:
                msg = g(ctx)
                if msg:
                    raise X402Error(GUARD_BLOCKED, str(msg))

    def _analyze(self, ticker: str, analysis_type: str, guard: Optional[Guard], emergency_stop: bool,
                 wallet_blocked: bool, reserve: float) -> dict:
        ctx: dict = {"symbol": ticker, "stage": "start"}
        self._refuse_if_guarded(ctx, guard, emergency_stop, wallet_blocked)
        try:
            if self.baw.status() != "CONNECTED":
                raise X402Error(SESSION, "Agentic Wallet is not signed in (reconnect the wallet)")
        except BawSessionExpired as e:
            raise X402Error(SESSION, f"wallet signed out: {e.args[0]}") from e
        except BawError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"baw wallet status failed: {e}") from e
        if self.calls_today() >= self.daily_calls:
            raise X402Error(DAILY_CAP, f"daily cap of {self.daily_calls} paid analyses reached (UTC day)")

        body = {"symbols": [ticker], "analysis_type": analysis_type}
        r = self._post_analyze(body)
        if r.status_code == 429:
            raise X402Error(RATE_LIMITED, "service rate limit (30 calls/wallet/hour)",
                            {"retry_after": r.headers.get("retry-after")}, 429)
        if r.status_code != 402:
            raise X402Error(UNEXPECTED_RESPONSE, f"expected HTTP 402 from unpaid POST, got {r.status_code}",
                            http_status=r.status_code)
        header = r.headers.get("payment-required")
        try:
            req = (r.json() or {}).get("paymentRequired")
        except ValueError:
            req = None
        if not isinstance(req, dict):
            req = _b64json(header) if header else None
        if not isinstance(req, dict) or not req.get("accepts"):
            raise X402Error(UNEXPECTED_RESPONSE, "402 without payment requirements")
        req_amounts: dict[str, float] = {}
        for a in req.get("accepts") or []:
            amt = _num(a.get("amount"))
            if amt is not None and str(a.get("payTo") or "").lower() == self.pay_to:
                req_amounts[str(a.get("asset") or "").lower()] = amt / 1e18  # all supported assets have 18 decimals

        try:
            preview = self.baw.x402_preview(header or req)
        except BawSessionExpired as e:
            raise X402Error(SESSION, f"wallet signed out: {e.args[0]}") from e
        except BawError as e:
            raise X402Error(PAYMENT_FAILED, f"baw x402-payment preview failed: {e}") from e
        opt = self._choose(preview, req_amounts)
        amount = self._amount(opt, req_amounts)
        token = str(opt.get("tokenSymbol") or "")
        if amount is None:
            raise X402Error(UNEXPECTED_RESPONSE, "payment option has no amount")
        if amount > self.max_price + 1e-12:
            raise X402Error(PRICE_OVER_CAP, f"price {amount:g} {token} is above the cap {self.max_price:g} "
                            "(STOCK_AGENT_X402_MAX_PRICE)", {"price": amount, "cap": self.max_price})
        q = self.quota()
        if q["left"] is not None and q["left"] < amount:
            raise X402Error(QUOTA_EXCEEDED, f"x402 quota left ${q['left']:g} < {amount:g}", {"quota": q})
        bal = _num(opt.get("currentBalance"))
        if bal is not None and bal < amount:
            raise X402Error(INSUFFICIENT_BALANCE, f"{token} balance {bal:g} < {amount:g}")
        if self._is_usdt(opt) and bal is not None and bal - amount < reserve:
            raise X402Error(EXIT_RESERVE, f"paying {amount:g} USDT would leave {bal - amount:g} USDT, below the "
                            f"{reserve:g} USDT kept for exits/fees")
        ctx.update(stage="before_sign", price=amount, token=token, balance=bal, quota_left=q["left"])
        self._refuse_if_guarded(ctx, guard, emergency_stop, wallet_blocked)

        # reserve today's slot before spending so a crash after sign still counts
        with self._lock:
            st = self._load()
            day = _utc_day()
            calls = {day: int((st.get("calls") or {}).get(day, 0))}  # keep only today
            if calls[day] >= self.daily_calls:
                raise X402Error(DAILY_CAP, f"daily cap of {self.daily_calls} paid analyses reached (UTC day)")
            calls[day] += 1
            st["calls"] = calls
            self._save(st)

        try:
            signed = self.baw.x402_sign(str(preview.get("paymentId")), int(opt.get("index") or 0))
        except BawError as e:
            self._release_slot()  # sign failed: nothing authorised
            if isinstance(e, BawSessionExpired):
                raise X402Error(SESSION, f"wallet signed out: {e.args[0]}") from e
            raise X402Error(PAYMENT_FAILED, f"baw x402-payment sign failed: {e}") from e
        self.limits_dirty = True
        hname = str(signed.get("paymentHeaderName") or PAYMENT_HEADER)
        hval = str(signed.get("paymentHeaderValue") or "")
        if not hval:
            raise X402Error(PAYMENT_FAILED, "baw sign returned no paymentHeaderValue")
        with self._lock:  # keep the proof so a crash/503 can replay the SAME one (never re-sign)
            st = self._load()
            st["pending"] = {"symbol": ticker, "body": body, "header_name": hname, "header_value": hval,
                             "payment_id": preview.get("paymentId"), "paid_usdt": amount, "token": token,
                             "signed_at": datetime.now(timezone.utc).isoformat(),
                             "expires_at": signed.get("signatureExpiresAt")}
            self._save(st)
        return self._replay(ticker, body, hname, hval, amount, token)

    def _release_slot(self) -> None:
        with self._lock:
            st = self._load()
            day = _utc_day()
            c = st.setdefault("calls", {})
            c[day] = max(0, int(c.get(day, 0)) - 1)
            self._save(st)

    def _replay(self, ticker: str, body: dict, hname: str, hval: str, amount: float, token: str,
                attempts: int = 4, wait_s: float = 5.0) -> dict:
        r = None
        for i in range(attempts):
            r = self._post_analyze(body, {hname: hval})
            if r.status_code == 503 and i < attempts - 1:  # settlement_pending: same proof again
                self._sleep(wait_s)
                continue
            break
        assert r is not None
        if r.status_code == 429:
            raise X402Error(RATE_LIMITED, "service rate limit on the paid replay; the signed proof is kept in the "
                            "x402 state file (`pending`) - replay it later with resume_pending(), do not re-sign",
                            {"retry_after": r.headers.get("retry-after")}, 429)
        if r.status_code not in (200, 201, 202):
            raise X402Error(PAYMENT_FAILED, f"paid replay -> HTTP {r.status_code}: {r.text[:300]} (proof kept in "
                            "`pending`; replay with resume_pending(), never re-sign)", http_status=r.status_code)
        d = r.json() or {}
        job_id, job_token = d.get("jobId"), d.get("jobToken")
        if not job_id or not job_token:
            raise X402Error(UNEXPECTED_RESPONSE, f"paid replay returned no jobId/jobToken: {str(d)[:300]}")
        settle = _b64json(r.headers.get("payment-response") or "") or {}
        tx = settle.get("transaction") or settle.get("txHash") or settle.get("transactionHash") or None
        rec = {"job_token": job_token, "symbol": ticker, "paid_usdt": amount, "token": token, "tx_hash": tx,
               "status": "pending", "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with self._lock:
                st = self._load()
                st.setdefault("jobs", {})[str(job_id)] = rec
                st.pop("pending", None)
                self._save(st)
        except Exception as e:  # paid and unrecoverable without these two values: surface them verbatim
            log.error("x402 PAID JOB NOT SAVED: jobId=%s jobToken=%s", job_id, job_token)
            raise X402Error(STATE_WRITE_FAILED, f"paid job could not be saved; keep these: jobId={job_id} "
                            f"jobToken={job_token}", {"job_id": job_id, "job_token": job_token}) from e
        out = {"job_id": str(job_id), "paid_usdt": amount, "tx_hash": tx}
        if self.on_paid:
            try:
                self.on_paid(out)
            except Exception:
                log.exception("x402 on_paid hook failed")
        return out

    def resume_pending(self) -> dict:
        """Replay a signed-but-unconfirmed proof from the state file (after a crash, 429 or 5xx). Never re-signs."""
        with self._lock:
            p = self._load().get("pending")
        if not p:
            raise X402Error(JOB_NOT_FOUND, "no pending signed payment")
        return self._replay(p["symbol"], p["body"], p["header_name"], p["header_value"], p["paid_usdt"], p["token"])

    def clear_limits_dirty(self) -> None:
        self.limits_dirty = False

    # ------------------------------------------------------------------ polling
    def _job_rec(self, job_id: str) -> dict:
        with self._lock:
            rec = (self._load().get("jobs") or {}).get(str(job_id))
        if not rec:
            raise X402Error(JOB_NOT_FOUND, f"no saved x402 job {job_id}")
        return rec

    def _set_job(self, job_id: str, **kw: Any) -> None:
        with self._lock:
            st = self._load()
            st.setdefault("jobs", {}).setdefault(str(job_id), {}).update(kw)
            self._save(st)

    def _report_path(self, job_id: str) -> Path:
        return self.state_path.parent / "x402_reports" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(job_id))}.md"

    def job(self, job_id: str) -> dict:
        """Bot 1's `GET /api/x402/jobs/{id}` shape: {status: pending|done|failed, report: markdown|null, error|null}.
        Raises X402Error(job_not_found) for unknown ids and (service_unavailable) when the poll itself fails; the
        saved job is untouched either way. Reports are cached next to the state file (presigned URLs expire)."""
        rec = self._job_rec(job_id)
        cached = self._report_path(job_id)
        if rec.get("status") == "done" and cached.is_file():
            return {"status": "done", "report": cached.read_text(encoding="utf-8"), "error": None}
        try:
            r = self.http.get(f"{self.base_url}/x402/jobs/{job_id}", headers={"X-Job-Token": rec["job_token"]})
        except httpx.HTTPError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"poll failed: {e}") from e
        if r.status_code in (401, 403, 404):
            return {"status": "failed", "report": None, "error": f"job lookup refused (HTTP {r.status_code})"}
        if r.status_code != 200:
            raise X402Error(SERVICE_UNAVAILABLE, f"poll -> HTTP {r.status_code}", http_status=r.status_code)
        d = r.json() or {}
        js = str(d.get("status") or "").lower()
        if js == "succeeded":
            url = d.get("downloadUrl")
            report = d.get("report") if isinstance(d.get("report"), str) else None
            if not report and url:
                try:
                    rr = self.http.get(url)
                    rr.raise_for_status()
                    report = rr.text
                except httpx.HTTPError as e:
                    raise X402Error(SERVICE_UNAVAILABLE, f"report download failed: {e}") from e
            if report:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(report, encoding="utf-8")
                self._set_job(job_id, status="done")
                return {"status": "done", "report": report, "error": None}
            return {"status": "pending", "report": None, "error": None}
        if js == "failed":
            err = str(d.get("error") or d.get("message") or "analysis failed")
            if d.get("retryable"):
                err += " (retryable: resume_job() retries it without a new charge)"
            self._set_job(job_id, status="failed", retryable=bool(d.get("retryable")))
            return {"status": "failed", "report": None, "error": err}
        return {"status": "pending", "report": None, "error": None}

    def resume_job(self, job_id: str) -> dict:
        """`POST /x402/jobs/{id}/resume` for a failed retryable job (no new charge). Returns the service JSON."""
        rec = self._job_rec(job_id)
        try:
            r = self.http.post(f"{self.base_url}/x402/jobs/{job_id}/resume", headers={"X-Job-Token": rec["job_token"]})
        except httpx.HTTPError as e:
            raise X402Error(SERVICE_UNAVAILABLE, f"resume failed: {e}") from e
        if r.status_code in (409, 410):
            raise X402Error(PAYMENT_FAILED, f"job {job_id} is permanently finished (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise X402Error(SERVICE_UNAVAILABLE, f"resume -> HTTP {r.status_code}", http_status=r.status_code)
        self._set_job(job_id, status="pending")
        return r.json() if r.content else {}

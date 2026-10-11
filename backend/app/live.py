"""LIVE helpers (Binance Agentic Wallet `baw` / BSC mainnet, chain id 56).

- Execution enrichment for the UI: tx_hash (contract), explorer_url, route, quote_expires_at, real_funds (LIVE only).
- Bot 3's LIVE risk sequence, duck-typed: risk.check(..., context=LiveContext(...)), risk.record_execution(res),
  risk.live_limits(pf, ctx). With an older risk (no `context` parameter) the app's own hard caps below stand in.
- Wallet (Bot 4, duck-typed: get_wallet() / get_balances() / wallet_address) and the live status block.
Nothing here places orders."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Optional

from backend.contracts import Portfolio, Quote, RiskCheck, Signal, Timeframe, TradingMode, utc_now

from .settings import Settings
from .wiring import call_with_supported, supported_kwargs

log = logging.getLogger("backend.app.live")

BSC_EXPLORER_TX = "https://bscscan.com/tx/"
BSC_CHAIN_ID = 56
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
GAS_ASSET, QUOTE_ASSET = "BNB", "USDT"
TIMEFRAME_DELTA = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
                   "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}


def bar_interval(tf: Timeframe) -> timedelta:
    return TIMEFRAME_DELTA.get(getattr(tf, "value", str(tf)), timedelta(hours=1))


# ---------------------------------------------------------------- executions for the UI
def explorer_url(tx_hash: Optional[str], mode: Any = TradingMode.LIVE) -> Optional[str]:
    """https://bscscan.com/tx/<hash> for a real (0x + 64 hex) hash of a LIVE execution; None otherwise."""
    m = getattr(mode, "value", mode)
    if not tx_hash or m != TradingMode.LIVE.value or not _TX_RE.match(tx_hash):
        return None
    return BSC_EXPLORER_TX + tx_hash


def enrich_payload(obj: Any, meta: Optional[Callable[[str], Optional[dict]]] = None) -> Any:
    """Walk a JSON payload. Every LIVE ExecutionResult-shaped dict gets route ('SWAP'|'RFQ'), real_funds=true,
    quote_expires_at (from the loop's record of the executed quote) and explorer_url (when the hash is on chain).
    SIM/PAPER executions are left exactly as the contract model, so strict clients keep parsing them."""
    if isinstance(obj, list):
        return [enrich_payload(v, meta) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: enrich_payload(v, meta) for k, v in obj.items()}
    if "client_order_id" in out and "tx_hash" in out and out.get("mode") == TradingMode.LIVE.value:
        out["route"] = out.get("path")
        out["real_funds"] = True
        m = ((meta(out.get("order_id")) if meta and out.get("order_id") else None)
             or (meta(out.get("client_order_id")) if meta and out.get("client_order_id") else None) or {})
        if m.get("quote_expires_at"):
            out["quote_expires_at"] = m["quote_expires_at"]
        url = explorer_url(out.get("tx_hash"), out.get("mode"))
        if url:
            out["explorer_url"] = url
        if m.get("price_impact_pct") is not None:
            out["price_impact_pct"] = m["price_impact_pct"]
        for k, v in (m.get("extras") or {}).items():  # Bot 4 executor extras win over app-computed values
            if k in ("explorer_url", "route", "real_funds", "gas", "price_impact_pct", "wallet_order_id") and v is not None:
                out[k] = v
    return out


add_explorer_urls = enrich_payload  # backwards-compatible name


# ---------------------------------------------------------------- Bot 3 LIVE risk (duck-typed)
def risk_allow_live(risk: Any) -> Optional[bool]:
    cfg = getattr(risk, "config", None)
    if callable(cfg):
        try:
            cfg = cfg()
        except TypeError:
            cfg = None
    if cfg is not None and hasattr(cfg, "allow_live"):
        return bool(cfg.allow_live)
    v = getattr(risk, "allow_live", None)
    return None if v is None else bool(v)


def apply_allow_live(risk: Any, settings: Settings) -> Optional[bool]:
    """Bot 3 reads RISK_ALLOW_LIVE itself (RiskConfig.from_env). For risks built with an explicit config or the
    stand-in, mirror it: only in LIVE, only when the two other switches are on, never switching it off."""
    if settings.trading_mode == TradingMode.LIVE and settings.risk_allow_live and settings.live_enabled \
            and risk_allow_live(risk) is False:
        cfg = getattr(risk, "config", None)
        if cfg is not None and hasattr(cfg, "allow_live") and callable(getattr(risk, "set_config", None)):
            risk.set_config(cfg.model_copy(update={"allow_live": True}))
        elif hasattr(risk, "allow_live"):
            try:
                risk.allow_live = True
            except AttributeError:
                pass
    return risk_allow_live(risk)


def risk_supports_context(risk: Any) -> bool:
    fn = getattr(risk, "check", None)
    return callable(fn) and "context" in supported_kwargs(fn, {"context": 1})


def make_live_context(**kw: Any) -> Any:
    """Bot 3's LiveContext when installed, else a plain namespace with the same fields (for duck-typed risks)."""
    try:
        from backend.risk import LiveContext  # type: ignore[attr-defined]
    except Exception:
        return SimpleNamespace(**{k: kw.get(k) for k in ("now", "data_timestamp", "bar", "bar_interval", "reference_price",
                                                           "quote", "balances", "daily_live_notional_usdt", "token_status",
                                                           "wallet_limits", "price_impact_pct")})
    fields = getattr(LiveContext, "model_fields", {})
    return LiveContext(**{k: v for k, v in kw.items() if not fields or k in fields})


def risk_live_limits(risk: Any, portfolio: Portfolio, ctx: Any = None) -> dict:
    fn = getattr(risk, "live_limits", None)
    if not callable(fn):
        return {}
    try:
        return dict(call_with_supported(fn, portfolio, ctx) if ctx is not None else fn(portfolio))
    except Exception:
        log.exception("risk.live_limits failed")
        return {}


def record_execution(risk: Any, res: Any) -> None:
    fn = getattr(risk, "record_execution", None)
    if callable(fn):
        try:
            fn(res)
        except Exception:
            log.exception("risk.record_execution failed")


# ---------------------------------------------------------------- wallet (Bot 4, duck-typed)
def _norm_balances(raw: Any) -> dict[str, float]:
    """{asset: amount} | [{asset|symbol|token, amount|free|balance|quantity}] | objects with those attributes."""
    out: dict[str, float] = {}
    if raw is None:
        return out
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for r in raw:
            g = (lambda k, r=r: r.get(k) if isinstance(r, dict) else getattr(r, k, None))
            asset = g("asset") or g("symbol") or g("token")
            amt = next((g(k) for k in ("amount", "free", "balance", "quantity") if g(k) is not None), 0)
            items.append((asset, amt))
    for k, v in items:
        if k:
            try:
                out[str(k)] = out.get(str(k), 0.0) + float(v)
            except (TypeError, ValueError):
                continue
    return out


def read_exchange_wallet(exchange: Any) -> tuple[Optional[str], Optional[dict[str, float]], str]:
    """(address, balances | None, source). balances is None when the client exposes no balance source."""
    address = getattr(exchange, "wallet_address", None)
    gw = getattr(exchange, "get_wallet", None)
    if callable(gw):
        w = gw()
        get = (lambda k: w.get(k)) if isinstance(w, dict) else (lambda k: getattr(w, k, None))
        return get("address") or address, _norm_balances(get("balances")), "exchange:get_wallet"
    gb = getattr(exchange, "get_balances", None)
    if callable(gb):
        return address, _norm_balances(gb()), "exchange:get_balances"
    return address, None, "none"


def wallet_provider(exchange: Any, mode: TradingMode) -> str:
    if mode == TradingMode.SIM:
        return "sim"
    if mode == TradingMode.PAPER:
        return "paper"
    ex = getattr(exchange, "executor", None) or getattr(exchange, "_executor", None)
    n = type(ex).__name__.lower() if ex is not None else ""
    return "web3_api" if "web3" in n else "baw"  # Bot 4's default LIVE executor is the Agentic Wallet CLI


# ---------------------------------------------------------------- app-side hard caps (risk without a LIVE profile)
def exposure(portfolio: Portfolio) -> float:
    return sum(p.quantity * (p.current_price or p.avg_entry_price) for p in portfolio.positions)


def app_live_checks(settings: Settings, signal: Signal, portfolio: Portfolio, notional: float,
                    balances: Optional[dict[str, float]], quote: Optional[Quote], last_price: Optional[float],
                    data_timestamp: Optional[datetime], daily_used: float = 0.0, now: Optional[datetime] = None) -> list[RiskCheck]:
    """The app's stand-in for Bot 3's LIVE profile (same RISK_LIVE_* limits). Used only when the installed risk
    module's check() has no `context` parameter."""
    now = now or utc_now()
    c: list[RiskCheck] = []

    def chk(name: str, ok: bool, detail: str) -> None:
        c.append(RiskCheck(name=name, passed=bool(ok), detail="" if ok else detail))

    s = settings
    chk("live_per_order_cap", notional <= s.live_max_order_usdt + 1e-9, f"order {notional:.2f} USDT > per-order cap {s.live_max_order_usdt:.2f} USDT")
    chk("live_daily_notional_cap", daily_used + notional <= s.live_max_daily_notional_usdt + 1e-9,
        f"today's LIVE buys {daily_used:.2f} + {notional:.2f} USDT > daily limit {s.live_max_daily_notional_usdt:.2f} USDT")
    loss_today = max(0.0, -portfolio.daily_pnl)
    chk("live_daily_loss_cap", loss_today < s.live_max_daily_loss_usdt,
        f"today's loss {loss_today:.2f} USDT reached the daily cap {s.live_max_daily_loss_usdt:.2f} USDT")
    exp = exposure(portfolio)
    chk("live_max_exposure", exp + notional <= s.live_max_exposure_usdt + 1e-9,
        f"exposure {exp:.2f} + {notional:.2f} USDT > max {s.live_max_exposure_usdt:.2f} USDT")
    if balances is not None:
        usdt, bnb = balances.get(QUOTE_ASSET, 0.0), balances.get(GAS_ASSET, 0.0)
        need = max(notional, quote.notional if quote else 0.0)  # Quote.notional is the USDT spent, fee included
        chk("live_usdt_balance", usdt + 1e-9 >= need, f"USDT balance {usdt:.2f} < {need:.2f} needed")
        chk("live_gas_reserve", bnb + 1e-12 >= s.live_min_bnb, f"BNB balance {bnb:.6f} < gas reserve {s.live_min_bnb}")
    if quote is not None:
        chk("live_quote", quote.symbol == signal.symbol and quote.side.value == "BUY" and quote.expires_at > now,
            f"quote for {quote.side.value} {quote.symbol} expiring {quote.expires_at.isoformat()} is not usable")
        if last_price:
            dev = abs(quote.price / last_price - 1) * 100
            chk("live_slippage", dev <= s.live_max_quote_deviation_pct,
                f"quote price {quote.price:g} is {dev:.2f}% from latest price {last_price:g} (max {s.live_max_quote_deviation_pct}%)")
    if data_timestamp is None:
        chk("live_stale_data", False, "no price/bar timestamp")
    else:
        age = (now - data_timestamp).total_seconds()
        chk("live_stale_data", age <= s.live_max_price_age_s, f"latest price is {age:.0f}s old (max {s.live_max_price_age_s:.0f}s)")
    return c


# ---------------------------------------------------------------- wallet settings (baw) + token status -> Bot 3 models
class WalletSettingsProvider:
    """The wallet's own limits (`baw wallet settings --json`) for Bot 3's LiveContext.wallet_limits.

    Source order: exchange.get_wallet_settings() (Bot 4 wiring baw) -> in LIVE ONLY, a `baw wallet settings --json`
    subprocess. Cached for `ttl_s`. Never runs a subprocess outside LIVE (SIM/PAPER/tests return None).
    On failure returns None and reports the error once per failure streak via `on_error`."""

    def __init__(self, exchange: Any, mode: TradingMode, ttl_s: float = 600.0, cmd: tuple[str, ...] = ("baw", "wallet", "settings", "--json"),
                 runner: Optional[Callable[[tuple[str, ...]], str]] = None, on_error: Optional[Callable[[str], None]] = None) -> None:
        self.exchange, self.mode, self.ttl_s, self.cmd = exchange, mode, ttl_s, cmd
        self.runner = runner or self._run
        self.on_error = on_error
        self._cached: Any = None
        self._raw: Optional[dict] = None
        self._at: Optional[datetime] = None
        self._failing = False
        self.last_error: Optional[str] = None

    def _argv(self) -> tuple[str, ...]:
        """`baw` resolved like Bot 4 (BAW_CMD / node ...\\agentic-wallet\\dist\\index.js on Windows / PATH)."""
        if self.cmd and self.cmd[0] == "baw":
            try:
                from backend.exchange.baw import resolve_baw_command  # type: ignore
                return tuple(resolve_baw_command()) + tuple(self.cmd[1:])
            except Exception:
                pass
        return self.cmd

    @staticmethod
    def _run(cmd: tuple[str, ...]) -> str:
        import subprocess
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=20, check=False)
        if p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} exited {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
        return p.stdout

    def raw(self) -> tuple[Optional[dict], bool]:
        """(payload, from_client). The client's get_wallet_settings() result carries its own fetched_at."""
        import json
        fn = getattr(self.exchange, "get_wallet_settings", None)
        if callable(fn):
            r = fn()
            if r is None:
                return None, True
            return (r if isinstance(r, dict) else (r.model_dump() if hasattr(r, "model_dump") else dict(r))), True
        if self.mode != TradingMode.LIVE:
            return None, False
        out = json.loads(self.runner(self._argv()))
        if isinstance(out, dict) and isinstance(out.get("data"), dict):  # tolerate {"success":..,"data":{..}} wrappers
            out = out["data"]
        return out, False

    def invalidate(self) -> None:
        """Re-read on next use (called after every execution: the wallet's daily quota just changed)."""
        self._at = None

    def get(self, force: bool = False) -> Any:
        now = utc_now()
        if not force and self._at is not None and (now - self._at).total_seconds() < self.ttl_s:
            return self._cached
        try:
            raw, from_client = self.raw()
            self._raw = raw
            self._cached = to_wallet_limits(raw, None if from_client else now) if raw is not None else None
            self._failing, self.last_error = False, None
        except Exception as e:
            self._cached, self.last_error = None, f"{type(e).__name__}: {e}"
            if not self._failing and self.on_error:
                self.on_error(f"wallet settings unavailable ({self.last_error}); LIVE uses risk's own caps")
            self._failing = True
        self._at = now
        return self._cached


def to_wallet_limits(raw: dict, fetched_at: Optional[datetime]) -> Any:
    """fetched_at=None: the payload (client.get_wallet_settings()) carries its own fetched_at (Bot 3 reads it)."""
    try:
        from backend.risk import WalletLimits  # type: ignore[attr-defined]
    except Exception:
        return None  # risk without wallet-limit support: nothing to pass
    return WalletLimits.from_baw(raw) if fetched_at is None else WalletLimits.from_baw(raw, fetched_at=fetched_at)


def to_token_status(obj: Any) -> Any:
    if obj is None:
        return None
    try:
        from backend.risk import TokenStatus  # type: ignore[attr-defined]
    except Exception:
        return obj
    if isinstance(obj, TokenStatus):
        return obj
    d = obj if isinstance(obj, dict) else (obj.model_dump() if hasattr(obj, "model_dump") else vars(obj))
    return TokenStatus.model_validate(d)


def quote_price_impact(quote: Any) -> Optional[float]:
    """Percent units (0.35 = 0.35%). Missing / null -> None (never 0)."""
    if quote is None:
        return None
    extra = getattr(quote, "model_extra", None) or {}
    for k in ("price_impact_pct", "price_impact_percent", "priceImpactPercent"):
        v = quote.get(k) if isinstance(quote, dict) else getattr(quote, k, extra.get(k))
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- exchange error classification
OFF_HOURS_CODES = {"40367": "Ondo market closed", "40369": "bStock market closed", "40374": "no liquidity / market closed"}
_CODE_RE = re.compile(r"\b(40367|40369|40374)\b")


def off_hours_code(err: Any) -> Optional[str]:
    """40367 / 40369 / 40374 from an exception (`.code`) or an error text; None otherwise."""
    if err is None:
        return None
    code = getattr(err, "code", None)
    if code is not None and str(code) in OFF_HOURS_CODES:
        return str(code)
    m = _CODE_RE.search(str(err))
    return m.group(1) if m else None


def is_session_expired(err: Any) -> bool:
    if err is None:
        return False
    names = f"{type(err).__name__} {getattr(err, 'code', '')} {err}".upper()
    return ("WALLET_SESSION_EXPIRED" in names or "SESSIONEXPIRED" in names.replace("_", "") or "SESSION EXPIRED" in names
            or "[WALLET_NOT_READY]" in names or "WALLETNOTREADY" in names.replace("_", ""))


def result_reason(res: Any) -> Optional[str]:
    """Bot 4 executor reason code ('[reason] message' error prefix); uses backend.exchange's result_reason if present."""
    try:
        from backend.exchange.baw_executor import result_reason as rr  # type: ignore
        r = rr(res)
        if r:
            return r
    except Exception:
        pass
    import re as _re
    m = _re.match(r"^\[([a-z_]+)\]", str(getattr(res, "error", None) or ""))
    return m.group(1) if m else None


def executor_extras(exchange: Any, client_order_id: Optional[str]) -> dict:
    """Bot 4: exchange.executor.extras[client_order_id] (explorer_url, route, real_funds, gas, ...); {} if absent."""
    ex = getattr(exchange, "executor", None)
    extras = getattr(ex, "extras", None)
    if not client_order_id or not isinstance(extras, dict):
        return {}
    v = extras.get(client_order_id)
    return dict(v) if isinstance(v, dict) else {}


def is_waiting_on_chain(res: Any) -> bool:
    st = getattr(getattr(res, "status", None), "value", getattr(res, "status", None))
    err = (getattr(res, "error", None) or "").upper()
    return st == "PENDING_CONFIRMATION" or "WAITING ON CHAIN" in err or "UNCONFIRMED" in err


def agent_identity() -> Optional[dict]:
    """Bot 5's backend/agent_studio get_agent_identity() passed through (all keys); None on None / ImportError."""
    try:
        from backend import agent_studio  # type: ignore[attr-defined]
        fn = getattr(agent_studio, "get_agent_identity", None)
        if not callable(fn):
            return None
        ident = fn()
    except Exception:
        return None
    if ident is None:
        return None
    d = ident if isinstance(ident, dict) else (ident.model_dump(mode="json") if hasattr(ident, "model_dump") else vars(ident))
    return dict(d)  # all keys as-is (erc8004_id, registry, studio_version, network, explorer_url, ...)

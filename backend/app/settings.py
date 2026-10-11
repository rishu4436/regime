"""Runtime settings from environment variables. Secrets are only read from env, never logged or returned."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from backend.contracts import DEFAULT_INITIAL_CAPITAL, TradingMode


def _bool(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class LiveModeNotAllowed(RuntimeError):
    pass


@dataclass
class Settings:
    trading_mode: TradingMode = TradingMode.SIM
    live_trading_enabled: bool = False   # LIVE_TRADING_ENABLED (app opt-in)
    binance_live_enabled: bool = False   # BINANCE_LIVE_ENABLED (exchange opt-in, Bot 4's factory checks it too)
    has_binance_keys: bool = False
    missing_binance_keys: tuple[str, ...] = ()  # names only (never values); filled by from_env
    # LIVE (see backend/app/GO_LIVE.md). Bot 3's risk reads the same RISK_LIVE_* env vars (RiskConfig.from_env); the
    # values here are the app's own hard caps, used only when the installed risk module has no LIVE profile.
    risk_allow_live: bool = False             # RISK_ALLOW_LIVE (3rd switch; Bot 3 reads it itself)
    wallet_address: Optional[str] = None      # BINANCE_WALLET_ADDRESS (display; Bot 4 uses it for RFQ quotes)
    live_max_order_usdt: float = 25.0         # RISK_LIVE_MAX_ORDER_USDT
    live_max_daily_loss_usdt: float = 25.0    # RISK_LIVE_MAX_DAILY_LOSS_USDT
    live_max_daily_notional_usdt: float = 100.0  # RISK_LIVE_MAX_DAILY_NOTIONAL_USDT
    live_max_exposure_usdt: float = 50.0      # RISK_LIVE_MAX_EXPOSURE_USDT
    live_min_bnb: float = 0.002               # RISK_LIVE_MIN_BNB
    live_max_price_age_s: float = 900.0       # RISK_LIVE_MAX_DATA_AGE_S
    live_max_quote_deviation_pct: float = 1.0 # RISK_LIVE_MAX_SLIPPAGE_PCT
    live_allowed_tokens: tuple[str, ...] = () # LIVE_ALLOWED_TOKENS=NVDAon,TSLAx (empty = every token the exchange lists)
    # 24/7
    loop_autostart: bool = False              # LOOP_AUTOSTART: resume the persisted agent and start the loop on boot
    state_dir: Optional[str] = None           # STATE_DIR (default LOG_DIR): agent_state.<mode>.json
    wallet_refresh_sec: float = 300.0         # WALLET_REFRESH_SEC: LIVE background wallet re-read (baw); orders always re-read first
    stall_intervals: float = 3.0              # ALERT when no tick for this many loop intervals
    default_agent_symbol: Optional[str] = None  # DEFAULT_AGENT_SYMBOL: autostart agent when none is saved (PAPER/LIVE: NVDAB)
    sim_bnb_balance: float = 0.05             # simulated wallet gas balance (SIM/PAPER)
    sim_seed: int = 42
    sim_replay_bars: int = 500
    window_bars: int = 200
    starting_capital: float = DEFAULT_INITIAL_CAPITAL
    loop_interval_seconds: float = 5.0
    pending_ttl_seconds: float = 300.0
    log_dir: Optional[str] = None
    use_fallbacks_only: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            trading_mode=TradingMode(e.get("TRADING_MODE", "SIM").upper()),
            live_trading_enabled=_bool(e.get("LIVE_TRADING_ENABLED")),
            binance_live_enabled=_bool(e.get("BINANCE_LIVE_ENABLED")),
            has_binance_keys=bool(e.get("BINANCE_API_KEY")) and bool(e.get("BINANCE_API_SECRET")),
            missing_binance_keys=tuple(k for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET") if not e.get(k)),
            sim_seed=int(e.get("SIM_SEED", "42")),
            sim_replay_bars=int(e.get("SIM_REPLAY_BARS", "500")),
            starting_capital=float(e.get("STARTING_CAPITAL", str(DEFAULT_INITIAL_CAPITAL))),
            loop_interval_seconds=float(e.get("LOOP_INTERVAL_SECONDS", "5")),
            pending_ttl_seconds=float(e.get("PENDING_TTL_SECONDS", "300")),
            log_dir=e.get("LOG_DIR") or None,
            use_fallbacks_only=_bool(e.get("USE_FALLBACKS_ONLY")),
            risk_allow_live=_bool(e.get("RISK_ALLOW_LIVE")),
            wallet_address=e.get("BINANCE_WALLET_ADDRESS") or None,
            live_max_order_usdt=float(e.get("RISK_LIVE_MAX_ORDER_USDT") or 25),
            live_max_daily_loss_usdt=float(e.get("RISK_LIVE_MAX_DAILY_LOSS_USDT") or 25),
            live_max_daily_notional_usdt=float(e.get("RISK_LIVE_MAX_DAILY_NOTIONAL_USDT") or 100),
            live_max_exposure_usdt=float(e.get("RISK_LIVE_MAX_EXPOSURE_USDT") or 50),
            live_min_bnb=float(e.get("RISK_LIVE_MIN_BNB") or 0.002),
            live_max_price_age_s=float(e.get("RISK_LIVE_MAX_DATA_AGE_S") or 900),
            live_max_quote_deviation_pct=float(e.get("RISK_LIVE_MAX_SLIPPAGE_PCT") or 1.0),
            live_allowed_tokens=tuple(t.strip() for t in (e.get("LIVE_ALLOWED_TOKENS") or "").split(",") if t.strip()),
            loop_autostart=_bool(e.get("LOOP_AUTOSTART")),
            default_agent_symbol=e.get("DEFAULT_AGENT_SYMBOL") or None,
            state_dir=e.get("STATE_DIR") or e.get("LOG_DIR") or None,
            wallet_refresh_sec=float(e.get("WALLET_REFRESH_SEC") or 300),
        )

    def validate(self) -> None:
        """LIVE is a separate, explicit double opt-in: LIVE_TRADING_ENABLED=true AND BINANCE_LIVE_ENABLED=true AND both
        Binance keys. Refused at startup with an error naming exactly what is missing."""
        if self.trading_mode == TradingMode.LIVE:
            missing = self.live_missing()
            if missing:
                raise LiveModeNotAllowed("TRADING_MODE=LIVE refused at startup; missing: " + ", ".join(missing)
                                         + ". LIVE needs LIVE_TRADING_ENABLED=true, BINANCE_LIVE_ENABLED=true,"
                                           " BINANCE_API_KEY and BINANCE_API_SECRET.")

    def live_missing(self) -> list[str]:
        missing = []
        if not self.live_trading_enabled:
            missing.append("LIVE_TRADING_ENABLED=true")
        if not self.binance_live_enabled:
            missing.append("BINANCE_LIVE_ENABLED=true")
        if not self.has_binance_keys:
            missing.extend(self.missing_binance_keys or ("BINANCE_API_KEY", "BINANCE_API_SECRET"))
        return missing

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode == TradingMode.LIVE and not self.live_missing()

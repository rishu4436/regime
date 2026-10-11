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

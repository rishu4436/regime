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
    live_trading_enabled: bool = False
    has_binance_keys: bool = False
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
            has_binance_keys=bool(e.get("BINANCE_API_KEY")) and bool(e.get("BINANCE_API_SECRET")),
            sim_seed=int(e.get("SIM_SEED", "42")),
            sim_replay_bars=int(e.get("SIM_REPLAY_BARS", "500")),
            starting_capital=float(e.get("STARTING_CAPITAL", str(DEFAULT_INITIAL_CAPITAL))),
            loop_interval_seconds=float(e.get("LOOP_INTERVAL_SECONDS", "5")),
            pending_ttl_seconds=float(e.get("PENDING_TTL_SECONDS", "300")),
            log_dir=e.get("LOG_DIR") or None,
            use_fallbacks_only=_bool(e.get("USE_FALLBACKS_ONLY")),
        )

    def validate(self) -> None:
        """LIVE is a separate, explicit opt-in: env flag AND both Binance keys."""
        if self.trading_mode == TradingMode.LIVE:
            missing = []
            if not self.live_trading_enabled:
                missing.append("LIVE_TRADING_ENABLED=true")
            if not self.has_binance_keys:
                missing.append("BINANCE_API_KEY and BINANCE_API_SECRET")
            if missing:
                raise LiveModeNotAllowed("TRADING_MODE=LIVE refused; requires " + " and ".join(missing))

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode == TradingMode.LIVE and self.live_trading_enabled and self.has_binance_keys

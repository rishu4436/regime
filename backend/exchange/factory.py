"""Pick the ExchangeClient from TRADING_MODE (default SIM).

Env vars (never commit values):
  TRADING_MODE              SIM (default) | PAPER | LIVE
  BINANCE_API_KEY           Web3 API key from https://web3.binance.com/en/dev-portal/project  (PAPER, LIVE)
  BINANCE_API_SECRET        matching secret key (HMAC)                                      (PAPER, LIVE)
  BINANCE_WALLET_ADDRESS    wallet that receives RFQ fills; REQUIRED to quote xStock (RFQ, else 40001) and
                            Ondo/BStock RFQ routes
  BINANCE_LIVE_ENABLED      must be exactly "true" to allow LIVE
  BINANCE_LIVE_EXECUTOR     agentic_wallet (default, `baw` CLI) | web3_api (needs a WalletSigner in code)
  BINANCE_LIVE_MAX_NOTIONAL client-side per-order cap in USDT for LIVE (default 25; with baw: BINANCE_LIVE_MAX_SELL_USDT)
  -- Agentic Wallet executor (baw_executor.py) --
  LIVE_TRADING_ENABLED, RISK_ALLOW_LIVE   must also be "true" or every baw order is refused (live_disabled)
  BINANCE_LIVE_MAX_ORDER_USDT    cap per BUY (default 25); the startup default for client.set_live_limits(max_order_usdt=)
  BINANCE_LIVE_HARD_MAX_ORDER_USDT  optional ceiling: set_live_limits refuses anything above it (unset = no ceiling)
  BINANCE_LIVE_MAX_SELL_USDT     hard cap per SELL/exit (default 2 x the buy cap)
  BINANCE_LIVE_MAX_SLIPPAGE_PCT  --slippage passed to every swap, percent 0-100 (default 1; never "auto")
  BINANCE_BNB_GAS_RESERVE        BNB kept for gas, warning below it (default 0.002)
  BAW_CMD                        baw argv: JSON list, path to dist/index.js (run with node) or a binary
  BAW_INTENT_FILE                order-intent journal (default ~/.teamwork/baw_intents.json)
  BINANCE_WEB3_BASE_URL     override base URL (default https://web3.binance.com/build)
  BINANCE_ENABLE_XSTOCK     "true" re-enables xStock (…x) tokens; off by default (zero BSC liquidity measured)
  BINANCE_PUBLIC_STATUS     "false" disables the public no-key RWA status/multiplier lookups (default on)
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

from backend.contracts import ExchangeClient, TradingMode

from .errors import ConfigError


def _api_from_env():
    from .web3_api import DEFAULT_BASE_URL, Web3ApiClient

    key, secret = os.getenv("BINANCE_API_KEY", "").strip(), os.getenv("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        raise ConfigError("BINANCE_API_KEY and BINANCE_API_SECRET are required for PAPER/LIVE")
    return Web3ApiClient(key, secret, base_url=os.getenv("BINANCE_WEB3_BASE_URL", DEFAULT_BASE_URL))


def get_exchange_client(mode: TradingMode | None = None, executor: Optional[object] = None) -> ExchangeClient:
    mode = mode or TradingMode(os.getenv("TRADING_MODE", "SIM").strip().upper() or "SIM")
    if mode == TradingMode.SIM:
        from .sim import SimulatedBinanceClient

        return SimulatedBinanceClient()

    from .live import BinanceWeb3Client

    wallet = os.getenv("BINANCE_WALLET_ADDRESS") or None
    public = None
    if os.getenv("BINANCE_PUBLIC_STATUS", "true").strip().lower() != "false":
        from .web3_api import PublicRwaClient

        public = PublicRwaClient()
    if mode == TradingMode.PAPER:
        return BinanceWeb3Client(_api_from_env(), mode=TradingMode.PAPER, wallet_address=wallet, public=public)

    # LIVE: explicit opt-in, keys, and an executor.
    if os.getenv("BINANCE_LIVE_ENABLED", "").strip().lower() != "true":
        raise ConfigError("LIVE trading is disabled: set BINANCE_LIVE_ENABLED=true to opt in")
    api = _api_from_env()
    if executor is None:
        kind = os.getenv("BINANCE_LIVE_EXECUTOR", "agentic_wallet").strip().lower()
        if kind == "agentic_wallet":
            from .baw import resolve_baw_command

            cmd = resolve_baw_command()
            if not (shutil.which(cmd[0]) or os.path.isfile(cmd[0])):
                raise ConfigError("`baw` not found: npm install -g @binance/agentic-wallet@1.10.0 (or set BAW_CMD), "
                                  "then `baw auth signin` on this machine")
            from .baw import BawCli
            from .baw_executor import BawExecutor

            executor = BawExecutor(cli=BawCli(cmd=cmd))
        else:
            raise ConfigError("BINANCE_LIVE_EXECUTOR=web3_api needs a WalletSigner: pass executor=Web3ApiExecutor(api, signer)")
    from .baw_executor import BawExecutor, BawLiveClient

    if isinstance(executor, BawExecutor):
        # The executor enforces BINANCE_LIVE_MAX_ORDER_USDT (buys) / BINANCE_LIVE_MAX_SELL_USDT (exits) itself.
        max_notional = float(os.getenv("BINANCE_LIVE_MAX_NOTIONAL") or executor.max_sell_usdt)
        if not wallet:
            try:
                wallet = executor.address()  # read-only; xStock/RFQ quotes need userWalletAddress
            except Exception:  # noqa: BLE001 - not signed in yet: get_wallet() fills it later
                wallet = None
        return BawLiveClient(api, mode=TradingMode.LIVE, wallet_address=wallet, executor=executor,
                             live_enabled=True, max_live_notional=max_notional, public=public)
    max_notional = float(os.getenv("BINANCE_LIVE_MAX_NOTIONAL", "25"))
    return BinanceWeb3Client(api, mode=TradingMode.LIVE, wallet_address=wallet, executor=executor,  # type: ignore[arg-type]
                             live_enabled=True, max_live_notional=max_notional, public=public)

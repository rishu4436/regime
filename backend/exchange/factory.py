"""Pick the ExchangeClient from TRADING_MODE (default SIM).

Env vars (never commit values):
  TRADING_MODE              SIM (default) | PAPER | LIVE
  BINANCE_API_KEY           Web3 API key from https://web3.binance.com/en/dev-portal/project  (PAPER, LIVE)
  BINANCE_API_SECRET        matching secret key (HMAC)                                      (PAPER, LIVE)
  BINANCE_WALLET_ADDRESS    wallet that receives RFQ fills; REQUIRED to quote xStock (RFQ, else 40001) and
                            Ondo/BStock RFQ routes
  BINANCE_LIVE_ENABLED      must be exactly "true" to allow LIVE
  BINANCE_LIVE_EXECUTOR     agentic_wallet (default, `baw` CLI) | web3_api (needs a WalletSigner in code)
  BINANCE_LIVE_MAX_NOTIONAL per-order cap in USDT for LIVE (default 25)
  BINANCE_WEB3_BASE_URL     override base URL (default https://web3.binance.com/build)
  BINANCE_ENABLE_XSTOCK     "true" re-enables xStock (…x) tokens; off by default (zero BSC liquidity measured)
  BINANCE_PUBLIC_STATUS     "false" disables the public no-key RWA status/multiplier lookups (default on)
"""
from __future__ import annotations

import os
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
            from .executors import AgenticWalletExecutor

            if not AgenticWalletExecutor.available():
                raise ConfigError("`baw` not found: npm install -g @binance/agentic-wallet, then `baw auth signin`")
            executor = AgenticWalletExecutor()
        else:
            raise ConfigError("BINANCE_LIVE_EXECUTOR=web3_api needs a WalletSigner: pass executor=Web3ApiExecutor(api, signer)")
    max_notional = float(os.getenv("BINANCE_LIVE_MAX_NOTIONAL", "25"))
    return BinanceWeb3Client(api, mode=TradingMode.LIVE, wallet_address=wallet, executor=executor,  # type: ignore[arg-type]
                             live_enabled=True, max_live_notional=max_notional, public=public)

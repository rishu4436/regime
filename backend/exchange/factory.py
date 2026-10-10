"""Bot 4 owns this. get_exchange_client() must return a SIM client unless TRADING_MODE says otherwise."""
from __future__ import annotations

import os

from backend.contracts import ExchangeClient, TradingMode


def get_exchange_client(mode: TradingMode | None = None) -> ExchangeClient:
    mode = mode or TradingMode(os.getenv("TRADING_MODE", "SIM"))
    if mode == TradingMode.SIM:
        from backend.mocks import MockExchangeClient  # placeholder until Bot 4 ships its SimulatedBinanceClient

        return MockExchangeClient()
    raise NotImplementedError(f"{mode.value} client not implemented yet (Bot 4)")

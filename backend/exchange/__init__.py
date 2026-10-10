"""Bot 4 - Binance Web3 API + Agentic Wallet layer. Implements backend.contracts.ExchangeClient."""
from .errors import ConfigError, ExchangeError, QuoteUnavailable, Web3ApiError
from .factory import get_exchange_client
from .sim import SimulatedBinanceClient

__all__ = ["ConfigError", "ExchangeError", "QuoteUnavailable", "Web3ApiError", "SimulatedBinanceClient",
           "get_exchange_client"]

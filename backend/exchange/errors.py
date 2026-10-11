"""Exchange-layer exceptions and Binance Web3 API error codes.

Error codes: https://web3.binance.com/en/dev-docs/authentication#error-codes and
https://web3.binance.com/en/dev-docs/products/trading-api/error-codes
"""
from __future__ import annotations

from typing import Optional

# Gateway / auth (authentication#error-codes)
INVALID_PARAMS = 40001
API_KEY_INVALID = 40101
INVALID_SIGNATURE = 40102
TIMESTAMP_EXPIRED_OR_REPLAY = 40103
NO_PERMISSION = 40104
RATE_LIMITED = 42900
REGION_RESTRICTED = 40301
# Trading API (trading-api/error-codes)
QUOTE_EXPIRED = 40401
SWAP_QUOTE_MISMATCH = 40462
ONDO_MAX_SINGLE_ORDER_LIMIT = 40366
ONDO_MARKET_STATE_NOT_TRADABLE = 40367
ONDO_STABLECOIN_PAIR_INVALID = 40368
BSTOCK_INVALID_TRADING_TIME = 40369
BSTOCK_INVALID_TRADING_PAIR = 40370
RWA_INSUFFICIENT_LIQUIDITY = 40374
ONDO_FROM_USD_AMOUNT_TOO_SMALL = 40375
ORDER_SUBMIT_IN_PROGRESS = 42901

MARKET_CLOSED_CODES = {ONDO_MARKET_STATE_NOT_TRADABLE, BSTOCK_INVALID_TRADING_TIME}


class ExchangeError(Exception):
    """Base class for exchange-layer errors."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class ConfigError(ExchangeError):
    """Missing/invalid configuration (keys, LIVE opt-in, wallet)."""


class Web3ApiError(ExchangeError):
    """Non-zero `code` in the OCResult envelope, or a non-2xx HTTP response."""

    def __init__(self, code: Optional[int], msg: str, http_status: Optional[int] = None) -> None:
        super().__init__(f"Binance Web3 API error {code}: {msg}", code)
        self.msg = msg
        self.http_status = http_status


class QuoteUnavailable(ExchangeError):
    """No executable quote (market closed, below minimum, no liquidity, unknown token...).

    `reason` is a status.QuoteReason value (market_closed, insufficient_liquidity, below_minimum, xstock_disabled,
    above_maximum) when the cause is market state rather than a bug/config problem; None otherwise."""

    def __init__(self, message: str, code: Optional[int] = None, reason: Optional[str] = None) -> None:
        super().__init__(message, code)
        self.reason = reason

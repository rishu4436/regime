"""Token registry: our symbols -> on-chain contracts.

Default addresses were read from the public Binance Web3 tokenized-stock list on 2026-10-11
(GET https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai?type=1|2|3,
documented in https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-tokenized-securities-info/SKILL.md).
They're a convenience for SIM/PAPER. Before LIVE, re-resolve them with `search_tokens()` (`/api/v1/dex/market/rwa/search`).
All defaults are on BNB Smart Chain (chain id 56) and quoted in BSC USDT, because Ondo tokens on BSC must pair with
USDT (trading-api/error-codes, 40368 ONDO_STABLECOIN_PAIR_INVALID) and Agentic Wallet supports BSC.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.contracts import TokenInfo, TokenType

BSC = "56"
ETHEREUM = "1"
SOLANA = "CT_501"

CHAIN_NAMES = {BSC: "BSC", ETHEREUM: "Ethereum", SOLANA: "Solana", "8453": "Base"}


@dataclass(frozen=True)
class Stablecoin:
    symbol: str
    address: str
    decimals: int


# Quote currency per chain (addresses from binance-agentic-wallet SKILL.md "Common Token Addresses").
QUOTE_ASSETS: dict[str, Stablecoin] = {
    BSC: Stablecoin("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
    ETHEREUM: Stablecoin("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    SOLANA: Stablecoin("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
}

# Same symbols/underlyings as backend.mocks.MOCK_TOKENS, but with real BSC deployments.
DEFAULT_TOKENS: list[TokenInfo] = [
    TokenInfo(symbol="NVDAon", name="NVIDIA (Ondo tokenized)", token_type=TokenType.ONDO, chain="BSC", chain_id=BSC,
              contract_address="0xa9ee28c80f960b889dfbd1902055218cba016f75", underlying="NVDA", decimals=18),
    TokenInfo(symbol="TSLAx", name="Tesla xStock", token_type=TokenType.XSTOCK, chain="BSC", chain_id=BSC,
              contract_address="0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0", underlying="TSLA", decimals=18),
    TokenInfo(symbol="AAPLB", name="Apple BStock", token_type=TokenType.BSTOCK, chain="BSC", chain_id=BSC,
              contract_address="0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a", underlying="AAPL", decimals=18),
    TokenInfo(symbol="SPYon", name="SPDR S&P 500 ETF (Ondo tokenized)", token_type=TokenType.ONDO, chain="BSC",
              chain_id=BSC, contract_address="0x6a708ead771238919d85930b5a0f10454e1c331a", underlying="SPY", decimals=18),
    TokenInfo(symbol="METAx", name="Meta xStock", token_type=TokenType.XSTOCK, chain="BSC", chain_id=BSC,
              contract_address="0x96702be57cd9777f835117a809c7124fe4ec989a", underlying="META", decimals=18),
]

# Token types whose /aggregator/quote needs userWalletAddress. xStock quotes are RFQ and fail with 40001
# "userWalletAddress is required for RFQ (xStock) quote" without one (verified 2026-10-11, read-only PAPER check).
WALLET_REQUIRED_TYPES = {TokenType.XSTOCK}

PLATFORM_TYPES = {"ondo": TokenType.ONDO, "bstock": TokenType.BSTOCK, "xstock": TokenType.XSTOCK}


def infer_token_type(symbol: str, platform_id: str | None = None) -> TokenType:
    """Ondo symbols end in 'on', xStocks in 'x', bStocks in 'B' (agentic-wallet SKILL.md, RWA list `type` 1/2/3)."""
    if platform_id and platform_id.lower() in PLATFORM_TYPES:
        return PLATFORM_TYPES[platform_id.lower()]
    if symbol.endswith("on"):
        return TokenType.ONDO
    if symbol.endswith("x"):
        return TokenType.XSTOCK
    if symbol.endswith("B"):
        return TokenType.BSTOCK
    return TokenType.OTHER


def to_base_units(amount: float, decimals: int) -> str:
    """Human amount -> smallest-unit integer string (Trading API `amount`), rounded down."""
    from decimal import ROUND_DOWN, Decimal

    return str(int((Decimal(str(amount)) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN)))


def from_base_units(raw: str | int | None, decimals: int) -> float:
    if raw in (None, ""):
        return 0.0
    from decimal import Decimal

    return float(Decimal(str(raw)) / (Decimal(10) ** decimals))

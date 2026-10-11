"""SIM token universe: real BSC bStocks / Ondo tokens taken from the live `GET /api/v1/dex/market/rwa/tokens`
response of 2026-10-11 (tests/samples/rwa_tokens_bsc.json): symbol, name, contract, underlying and the then-current
tokenPrice as the SIM start price; volume_24h is the real DEX volume from `/market/price-info` where it had one
(tests/samples/price_info_bsc.json), else a 25,000 USDT placeholder. Regimes are a fixed SIM spread so Auto-pick has
something to rank. They're resolvable by every SIM method (candles, price, quotes, fills) but are NOT returned by
search_tokens("") (the loop/feed subscribe to that list), only by list_tokens() and named searches."""
from __future__ import annotations

from backend.contracts import Regime, TokenInfo, TokenType

from .tokens import BSC

PLACEHOLDER_VOLUME = 25_000.0

# symbol, name, type, contract (BSC), underlying, SIM start price (real tokenPrice), real DEX 24h volume or None
_ROWS = [
    ("TSLAB", "Tesla (bStocks)", "BSTOCK", "0x5b1910eaad6450e50f816082aa078c41f10c292f", "TSLA", 382.34, 87177.94),
    ("MSFTB", "Microsoft (bStocks)", "BSTOCK", "0x80106cb3ead06659a5ad19df39d9b4733863b9b0", "MSFT", 535.43, 2891.71),
    ("METAB", "Meta (bStocks)", "BSTOCK", "0x7425889fe94f9d693e8daefe88bcced6acfef4c0", "META", 719.62, 117766.06),
    ("AMDB", "Advanced Micro Devices (bStocks)", "BSTOCK", "0x75fd4cf6f8392e41e70391d60c90c0d5211603a1", "AMD", 608.64, 72.88),
    ("TSMB", "TSMC (bStocks)", "BSTOCK", "0xab78b89b5bb00236be0b4b20704cbfa04efc711c", "TSM", 454.29, 1427.65),
    ("AVGOB", "Broadcom (bStocks)", "BSTOCK", "0x76682c454467b3a1150ad8b6a92fc5ee2c21d7ed", "AVGO", 363.26, 1152.84),
    ("PLTRB", "Palantir (bStocks)", "BSTOCK", "0x0ca5d51d0277bd006fd9607d3e560785ebad8222", "PLTR", 209.28, None),
    ("COINB", "Coinbase (bStocks)", "BSTOCK", "0x585bde7c54abb5ccd7791f923d6c2187635f3952", "COIN", 180.81, None),
    ("MSTRB", "Strategy (bStocks)", "BSTOCK", "0xe87afb3076aeb0f9b14e368de8145ae6a2826a14", "MSTR", 155.37, 52432.41),
    ("GOOGLB", "Alphabet (bStocks)", "BSTOCK", "0x3f53de71c126bdabae20f9cd64848d317f6c3238", "GOOGL", 351.06, 171090.5),
    ("QQQB", "Invesco QQQ Trust (bStocks)", "BSTOCK", "0x205812cdbed920aff76c6580abd681a46d11efc7", "QQQ", 751.44, 3759228.78),
    ("SPYB", "SPY (bStocks)", "BSTOCK", "0x7138b48df7d98d7e3cc221bfe7192d0a178182d8", "SPY", 780.5, 172728.44),
    ("INTCB", "Intel (bStocks)", "BSTOCK", "0xe614e2fc6c787035ff51f452e8e826bfd32d5283", "INTC", 104.7, 5563.76),
    ("ORCLB", "Oracle (bStocks)", "BSTOCK", "0x4684d9887fc1c71cba7bab8e88835cec217eb598", "ORCL", 141.41, 876.0),
    ("CRCLB", "Circle Internet Group (bStocks)", "BSTOCK", "0x80f3d493ebce97e343c53d29a137942416b4ffc0", "CRCL", 85.59, 238766.38),
    ("HOODB", "Robinhood (bStocks)", "BSTOCK", "0xa394dcea3fd3847fd793afbfd163e2e3858b7c65", "HOOD", 109.37, 4648.41),
    ("MUB", "Micron Technology (bStocks)", "BSTOCK", "0xcdf2f3e0fa43c47a6662a91c9e4a7c5f69762699", "MU", 1033.88, 1107.32),
    ("AAPLon", "Apple (Ondo)", "ONDO", "0x390a684ef9cade28a7ad0dfa61ab1eb3842618c4", "AAPL", 338.72, 8066.09),
    ("AMZNon", "Amazon (Ondo)", "ONDO", "0x4553cfe1c09f37f38b12dc509f676964e392f8fc", "AMZN", 262.4, 5951.43),
    ("MSFTon", "Microsoft (Ondo)", "ONDO", "0x6bfe75d1ad432050ea973c3a3dcd88f02e2444c3", "MSFT", 540.33, 916.14),
    ("METAon", "Meta Platforms (Ondo)", "ONDO", "0xd7df5863a3e742f0c767768cdfcb63f09e0422f6", "META", 722.58, 2787.55),
    ("COINon", "Coinbase (Ondo)", "ONDO", "0xf8589b526fdd65f7f301c605a6e04f0f1b4b3620", "COIN", 180.97, 457.8),
    ("AVGOon", "Broadcom (Ondo)", "ONDO", "0x0ed2e3180edf393e6bf8db124bd15ddd54de150a", "AVGO", 367.8, 3053.82),
    ("AMDon", "AMD (Ondo)", "ONDO", "0x9f16e46c73b43bdb70861247d537bee4ea18f639", "AMD", 609.25, 127.89),
    ("GOOGLon", "Alphabet Class A (Ondo)", "ONDO", "0x091fc7778e6932d4009b087b191d1ee3bac5729a", "GOOGL", 352.85, 10281.01),
    ("QQQon", "Invesco QQQ (Ondo)", "ONDO", "0x0cde6936d305d5b34667fc46425e852efd73559a", "QQQ", 757.39, 5084.77),
]

_REGIME_CYCLE = [Regime.STRONG_UPTREND, Regime.MILD_UPTREND, Regime.SIDEWAYS, Regime.OVERSOLD_PULLBACK,
                 Regime.HIGH_VOLATILITY, Regime.DOWNTREND]

SIM_UNIVERSE: list[TokenInfo] = [
    TokenInfo(symbol=s, name=n, token_type=TokenType(t), chain="BSC", chain_id=BSC, contract_address=a,
              underlying=u, decimals=18, price=p, volume_24h=v if v else PLACEHOLDER_VOLUME)
    for s, n, t, a, u, p, v in _ROWS
]
SIM_UNIVERSE_REGIMES: dict[str, Regime] = {t.symbol: _REGIME_CYCLE[i % len(_REGIME_CYCLE)]
                                           for i, t in enumerate(SIM_UNIVERSE)}
# Real DEX volume for the default SIM tokens (NVDAB from price-info 2026-10-11; others placeholders, xStock 0).
SIM_DEFAULT_VOLUME = {"NVDAB": 378131.88, "NVDAon": 7215.86}

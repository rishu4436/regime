import pytest

from backend.contracts import ExchangeClient, TradingMode
from backend.exchange import ConfigError, SimulatedBinanceClient, get_exchange_client
from backend.exchange.executors import AgenticWalletExecutor
from backend.exchange.live import BinanceWeb3Client

ENV = ["TRADING_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_LIVE_ENABLED", "BINANCE_LIVE_EXECUTOR",
       "BINANCE_WALLET_ADDRESS", "BINANCE_LIVE_MAX_NOTIONAL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_default_is_sim():
    c = get_exchange_client()
    assert isinstance(c, SimulatedBinanceClient) and isinstance(c, ExchangeClient) and c.mode == TradingMode.SIM


def test_paper_needs_keys(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    with pytest.raises(ConfigError):
        get_exchange_client()
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    c = get_exchange_client()
    assert isinstance(c, BinanceWeb3Client) and c.mode == TradingMode.PAPER


def test_live_needs_explicit_flag(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    with pytest.raises(ConfigError, match="BINANCE_LIVE_ENABLED"):
        get_exchange_client(TradingMode.LIVE)
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    monkeypatch.setenv("BINANCE_LIVE_MAX_NOTIONAL", "5")
    c = get_exchange_client(TradingMode.LIVE, executor=AgenticWalletExecutor(runner=lambda a: "{}"))
    assert c.mode == TradingMode.LIVE and c.max_live_notional == 5


def test_live_web3_api_executor_needs_signer(monkeypatch):
    for k, v in {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "BINANCE_LIVE_ENABLED": "true",
                 "BINANCE_LIVE_EXECUTOR": "web3_api"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match="WalletSigner"):
        get_exchange_client(TradingMode.LIVE)

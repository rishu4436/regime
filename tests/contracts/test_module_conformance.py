"""Each bot's main class must keep satisfying its Protocol (Bot 6 wires them by these names)."""
from backend.contracts import ExchangeClient, Explainer, RiskManager, StrategyEngine
from backend.engine.engine import DefaultStrategyEngine
from backend.exchange.factory import get_exchange_client
from backend.llm.explainer import DefaultExplainer
from backend.risk.manager import DefaultRiskManager


def test_modules_satisfy_protocols():
    assert isinstance(DefaultStrategyEngine(), StrategyEngine)
    assert isinstance(DefaultRiskManager(), RiskManager)
    assert isinstance(DefaultExplainer(), Explainer)
    assert isinstance(get_exchange_client(), ExchangeClient)

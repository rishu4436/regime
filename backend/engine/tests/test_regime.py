import pytest

from backend.contracts import Regime
from backend.engine import DefaultStrategyEngine
from backend.engine.regime import MIN_BARS, detect_regime
from backend.mocks import MockExchangeClient, generate_candles

from .helpers import flat


@pytest.mark.parametrize("regime", list(Regime))
def test_detects_each_mock_regime_default_seed(regime):
    ra = detect_regime(generate_candles(regime, n=250, seed=42))
    assert ra.regime == regime, ra.reason
    assert 0 <= ra.confidence <= 1
    assert ra.reason
    for key in ("rsi", "sma_slope_pct", "volatility_pct", "atr_pct", "dist_from_ema_pct"):
        assert key in ra.features


def test_accuracy_over_many_seeds():
    total = hits = 0
    for regime in Regime:
        for seed in range(25):
            for n in (100, 250):
                total += 1
                hits += detect_regime(generate_candles(regime, n=n, seed=seed)).regime == regime
    assert hits / total >= 0.97


def test_mock_exchange_default_regimes():
    ex, eng = MockExchangeClient(), DefaultStrategyEngine()
    expected = {
        "NVDAon": Regime.STRONG_UPTREND,
        "TSLAx": Regime.HIGH_VOLATILITY,
        "AAPLB": Regime.SIDEWAYS,
        "SPYon": Regime.MILD_UPTREND,
        "METAx": Regime.OVERSOLD_PULLBACK,
    }
    for sym, reg in expected.items():
        assert eng.detect_regime(ex.get_candles(sym, limit=200)).regime == reg


def test_insufficient_and_empty_data():
    ra = detect_regime(flat(MIN_BARS - 1))
    assert ra.regime == Regime.SIDEWAYS and ra.confidence == pytest.approx(0.1)
    assert detect_regime([]).confidence == 0.0


def test_flat_market_is_sideways():
    ra = detect_regime(flat(100))
    assert ra.regime == Regime.SIDEWAYS
    assert ra.features["rsi"] == 50.0


def test_deterministic_and_uses_candle_time():
    cs = generate_candles(Regime.DOWNTREND, n=120)
    a, b = detect_regime(cs), detect_regime(cs)
    assert a.model_dump() == b.model_dump()
    assert a.timestamp == cs[-1].timestamp

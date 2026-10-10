"""Skeleton. Bot 2 replaces the NotImplementedError bodies. Keep the class name/constructor stable for Bot 6."""
from __future__ import annotations

from typing import Sequence

from backend.contracts import (
    AutoConfig, BacktestResult, Candle, ManualStrategy, Portfolio, PresetName, Regime, RegimeAnalysis,
    Signal, StrategySelection, TraderStyle,
)


class DefaultStrategyEngine:
    def detect_regime(self, candles: Sequence[Candle]) -> RegimeAnalysis:
        raise NotImplementedError

    def select_strategy(self, regime: Regime, style: TraderStyle, enabled_presets: Sequence[PresetName]) -> StrategySelection:
        raise NotImplementedError

    def evaluate_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        raise NotImplementedError

    def evaluate_auto(self, config: AutoConfig, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        raise NotImplementedError

    def backtest_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], initial_capital: float) -> BacktestResult:
        raise NotImplementedError

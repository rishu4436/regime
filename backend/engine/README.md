# `backend/engine` — Bot 2 — Strategy & Regime Engine

**Owner:** Bot 2 — Strategy & Regime Engine. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Own all strategy evaluation and regime logic.

- Implement Regime Detector with the regimes listed above
- Implement all pre-set agents (DCA, Smart DCA, Buy the Dip, Trend, Grid)
- Trader style mapping (Patient / Balanced / Active)
- Manual strategy rule evaluator (must match simulator logic)
- Strict single-strategy-at-a-time execution in Auto Mode
- Clear structured decision output (regime, strategy, reason, action)

## Interface it must implement
Implement `backend.contracts.StrategyEngine` in `engine.py` (`DefaultStrategyEngine`):

```python
detect_regime(candles) -> RegimeAnalysis
select_strategy(regime, style, enabled_presets) -> StrategySelection   # exactly one preset or None (stay out)
evaluate_manual(strategy: ManualStrategy, candles, portfolio) -> Signal
evaluate_auto(config: AutoConfig, candles, portfolio) -> Signal      # regime -> map(regime, style) -> evaluate that preset only
backtest_manual(strategy, candles, initial_capital) -> BacktestResult
```
Pure functions: no I/O, no execution, no risk decisions (Bot 3 does those). Indicator formulas, condition
combination (AND/OR, skipped warm-up conditions), exit priority and cooldown semantics are specified in the
docstrings of `backend/contracts/models.py` and in `reference/README.md`. `backtest_manual` with a
simulator-equivalent config must reproduce the simulator's trade list on the same candles.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

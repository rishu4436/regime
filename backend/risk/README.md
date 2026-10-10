# `backend/risk` — Bot 3 — Risk & Control Layer

**Owner:** Bot 3 — Risk & Control Layer. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Enforce safety on every potential trade.

- Position size and max exposure limits
- Stop-loss / trailing stop enforcement
- Cooldowns and loss limits
- Confirmation mode vs full auto
- Global Pause / Resume / Emergency Stop
- Ability to block any signal from Bot 2

## Interface it must implement
Implement `backend.contracts.RiskManager` in `manager.py` (`DefaultRiskManager`):

```python
check(signal, portfolio, control) -> RiskVerdict        # can always force STAY_OUT (approved=False + reasons)
check_exits(portfolio, prices: dict[str, float]) -> list[Signal]   # SL / TP / trailing -> SELL signals
get_control_state() -> ControlState
pause(reason="") / resume() / emergency_stop(reason="") / set_confirmation_mode(enabled) -> ControlState
```
Every check must appear in `RiskVerdict.checks` (name, passed, detail) so decisions are explainable.
`requires_confirmation=True` when `ControlState.confirmation_mode` is on. Emergency stop blocks everything until reset.
LIVE mode must be blocked unless explicitly enabled by config.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

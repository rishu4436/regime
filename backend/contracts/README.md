# `backend/contracts` — coordinator only

Single source of truth for all data shapes (`models.py`, `enums.py`) and module interfaces (`interfaces.py`).
Bots import from here (`from backend.contracts import ManualStrategy, StrategyEngine, ...`) but never edit it.
After any change: `python scripts/gen_contracts.py` → regenerates `contracts/schema/*.json` and `frontend/src/contracts.ts`.
`ManualStrategy` defaults and semantics mirror `reference/full_strategy_builder_simulation.html` (see `reference/README.md`).

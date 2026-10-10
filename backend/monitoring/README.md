# `backend/monitoring` — Bot 6 — Integration, Testing & Monitoring (monitoring)

**Owner:** Bot 6 — Integration, Testing & Monitoring. This bot edits only this directory (plus `tests/e2e` and the other Bot 6 dirs).

## Scope (verbatim from the spec PDF)
**Goal:** Assemble, test, and observe the full system.

- Logging of decisions, risk checks, and executions
- Alerts and performance summaries

## Interface it must implement
Structured JSON logging of every `Decision`, `RiskVerdict` and `ExecutionResult` (one `Event` per line),
alert rules (e.g. emergency stop, consecutive losses, quote expiries) and performance summaries (PnL, win rate,
drawdown) computed from logged decisions.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

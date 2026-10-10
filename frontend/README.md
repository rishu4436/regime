# `frontend` — Bot 1 — UI & Manual Builder

**Owner:** Bot 1 — UI & Manual Builder. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Build the complete user interface for both Manual and Auto modes.

- Full Manual Strategy Builder matching the simulator (all indicators, AND/OR, exits, risk, filters)
- Agent selection + trader style selector for Auto Mode
- Dashboard showing current regime, active strategy, open positions, PnL, last decision reason
- Simulation results view
- Confirmation, pause, and override controls
- Reference the existing full_strategy_builder_simulation.html for UI and interaction patterns

## Interface it must implement
Consumes the REST/WebSocket API served by `backend/app` (Bot 6). All request/response types come from
`src/contracts.ts`, which is **generated** from `backend/contracts` — never edit it by hand
(`python scripts/gen_contracts.py` regenerates it). Build `ManualStrategy` JSON exactly as typed there; defaults
mirror `reference/full_strategy_builder_simulation.html`.

## Definition of done
- React + Vite + TypeScript app in this directory; `npm run build` passes with zero TS errors.
- Builder covers every `ManualStrategy` field (see `reference/README.md` for the simulator mapping), Auto Mode
  config (`AutoConfig`), dashboard (`StatusSnapshot`, `Decision`), simulation results (`BacktestResult`) and controls (`ControlState`).
- Works against the SIM backend (`uvicorn backend.app.main:app`), with a mock-data fallback when the backend is down.
- Dev: `npm run dev` (proxy `/api` and `/ws` to `http://localhost:8000`, configured in `vite.config.ts`).
- No edits outside `/frontend`. Contract changes go through the coordinator.

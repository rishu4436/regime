# Regime — team workflow (6 bots + coordinator)

## Ownership — the golden rule
Each bot edits **only its own directory**. Everything else is read-only for it.

| Bot | Role | Owns |
|---|---|---|
| 1 | UI & Manual Builder | `frontend/` (except the generated `frontend/src/contracts.ts`) |
| 2 | Strategy & Regime Engine | `backend/engine/` |
| 3 | Risk & Control | `backend/risk/` |
| 4 | Binance Web3 API + Agentic Wallet | `backend/exchange/` |
| 5 | LLM & Explanation (Grok) | `backend/llm/` |
| 6 | Integration, Testing & Monitoring | `backend/app/`, `backend/monitoring/`, `tests/e2e/` |
| coordinator | contracts, mocks, root config | `backend/contracts/`, `backend/mocks/`, `contracts/`, `scripts/`, `reference/`, root files, `.github/` |

**Contracts changes go through the coordinator.** If you need a new field/model/method, do not edit
`backend/contracts/` on your branch. Ask the coordinator (describe the change and why); the coordinator updates
`backend/contracts`, runs `python scripts/gen_contracts.py`, pushes to `main`, and you rebase.
Until then, keep extra data inside your own module (e.g. `features: dict` / private helpers).

Need an implementation from another bot that isn't ready? Use `backend.mocks` or a local fake inside your own `tests/`.

## Your worktree
Each bot has its own git worktree (a separate checkout sharing one repo):

```
/workspace/teamwork-bot1   branch bot1/main   (frontend)
/workspace/teamwork-bot2   branch bot2/main   (backend/engine)
/workspace/teamwork-bot3   branch bot3/main   (backend/risk)
/workspace/teamwork-bot4   branch bot4/main   (backend/exchange)
/workspace/teamwork-bot5   branch bot5/main   (backend/llm)
/workspace/teamwork-bot6   branch bot6/main   (backend/app, backend/monitoring, tests/e2e)
```
`/workspace/teamwork` (branch `main`) belongs to the coordinator — bots never work there.
Python deps are installed in the shared venv: `source /workspace/.venv/bin/activate` (pytest's `pythonpath=.` makes
`import backend...` resolve to *your* worktree). Frontend: run `npm install` inside `frontend/` of your own worktree.

## Loop
```bash
cd /workspace/teamwork-bot<N>
source /workspace/.venv/bin/activate
git fetch origin && git rebase origin/main          # pick up contract updates often
git switch -c bot<N>/<topic>                         # optional topic branch; bot<N>/main is fine for the first PR
# ... code, only inside your directory ...
pytest                                               # must pass (whole suite)
(cd frontend && npm run build)                       # Bot 1 (and Bot 6 before integration PRs)
git add <your dir> && git commit -m "bot<N>: <what>" # commit small and often
git push -u origin HEAD
gh pr create --base main --title "bot<N>: <topic>" --body "<what / how tested>"
```
- Before pushing: `git diff --stat origin/main...HEAD` must show only files in your directory.
- Never force-push `main`, never commit `.env`, keys, `node_modules`, `__pycache__` or venvs.
- Keep PRs small; CI (`.github/workflows/ci.yml`) runs `gen_contracts --check`, `pytest` and the frontend build.
- After a PR merges: `git fetch origin && git rebase origin/main` (or start a new `bot<N>/<topic>` branch from `origin/main`).

## Tests
- Unit tests: `<your dir>/tests/test_*.py` (backend) — run with `pytest` from the worktree root.
- E2E (Bot 6): `tests/e2e/`. Coordinator tests: `tests/contracts`, `tests/mocks`.
- Everything must pass in `TRADING_MODE=SIM` with no network and no API keys.

## Secrets
Only via env vars (see `.env.example`): `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `XAI_API_KEY`, `TRADING_MODE=SIM`.
LIVE trading is a separate, explicit opt-in; default is SIM.

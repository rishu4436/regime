#!/usr/bin/env bash
# One-command local run: backend (FastAPI :8000) + frontend (Vite :5173, proxies /api /ws /health).
#   backend/app/deploy/run.sh                 # SIM mode, both services
#   backend/app/deploy/run.sh --backend-only  # API only (http://localhost:8000/docs)
# Mode/secrets come from the environment or a git-ignored .env at the repo root (TRADING_MODE=SIM by default).
# LIVE additionally needs LIVE_TRADING_ENABLED=true and BINANCE_API_KEY/BINANCE_API_SECRET (refused otherwise).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
BACKEND_ONLY=0
[[ "${1:-}" == "--backend-only" ]] && BACKEND_ONLY=1

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export TRADING_MODE="${TRADING_MODE:-SIM}"
export LOG_DIR="${LOG_DIR:-$ROOT/logs}"
PORT="${PORT:-8000}"

if [[ -n "${VENV:-}" ]]; then . "$VENV/bin/activate"
elif [[ -d /workspace/.venv ]]; then . /workspace/.venv/bin/activate
else
  [[ -d .venv ]] || python3 -m venv .venv
  . .venv/bin/activate
  python -c "import fastapi, uvicorn" 2>/dev/null || pip install -q -e ".[dev]"
fi

echo "==> TRADING_MODE=$TRADING_MODE  logs: $LOG_DIR  api: http://localhost:$PORT/docs"
uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" &
API_PID=$!
trap 'kill $API_PID ${UI_PID:-} 2>/dev/null || true' EXIT INT TERM

if [[ $BACKEND_ONLY -eq 0 ]]; then
  ( cd frontend && { [[ -d node_modules ]] || npm install --no-audit --no-fund; } && npm run dev -- --host 0.0.0.0 ) &
  UI_PID=$!
  echo "==> UI: http://localhost:5173"
fi
wait

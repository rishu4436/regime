#!/usr/bin/env bash
# Regime - one-command local run: backend (FastAPI :8000) + dashboard (Vite :5173, proxies /api /ws /health).
#   backend/app/deploy/run.sh                     # SIM mode, both services; the dashboard talks to the real backend
#   backend/app/deploy/run.sh --mock              # dashboard on built-in mock data instead (no backend calls)
#   backend/app/deploy/run.sh --backend-only      # API only (http://localhost:8000/docs)
#   backend/app/deploy/run.sh --supervised        # 24/7: restart the backend whenever it exits (backoff 2..60 s)
#   backend/app/deploy/run.sh --supervised --backend-only
# Mode/secrets come from the environment or a git-ignored .env at the repo root (TRADING_MODE=SIM by default).
# LIVE additionally needs LIVE_TRADING_ENABLED=true, BINANCE_LIVE_ENABLED=true, RISK_ALLOW_LIVE=true and
# BINANCE_API_KEY/BINANCE_API_SECRET (refused at startup otherwise, naming what is missing). See backend/app/GO_LIVE.md.
# --supervised also sets LOOP_AUTOSTART=true (resume the saved agent, or Auto on DEFAULT_AGENT_SYMBOL / NVDAB in
# PAPER/LIVE). A config error (LIVE refused) is checked before the first start and exits 3 instead of looping.
# The dashboard listens on 127.0.0.1 only (UI_HOST=0.0.0.0 to share it on your LAN; wallet sign-in stays localhost-only).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
BACKEND_ONLY=0
SUPERVISED=0
UI_MOCK=0
for a in "$@"; do
  case "$a" in
    --backend-only) BACKEND_ONLY=1 ;;
    --mock) UI_MOCK=1 ;;
    --supervised) SUPERVISED=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
export TRADING_MODE="${TRADING_MODE:-SIM}"
export LOG_DIR="${LOG_DIR:-$ROOT/logs}"
export STATE_DIR="${STATE_DIR:-$LOG_DIR}"
PORT="${PORT:-8000}"
mkdir -p "$LOG_DIR"

if [[ -n "${VENV:-}" ]]; then . "$VENV/bin/activate"
elif [[ -d /workspace/.venv ]]; then . /workspace/.venv/bin/activate
else
  [[ -d .venv ]] || python3 -m venv .venv
  . .venv/bin/activate
  # eth_account is in the check so an older .venv picks up new deps after a git pull.
  # Agent Studio "Register" also needs the optional SDK: pip install -e ".[studio]"
  python -c "import fastapi, uvicorn, eth_account" 2>/dev/null || pip install -q -e ".[dev]"
fi

if [[ "$TRADING_MODE" == "LIVE" ]] && ! command -v baw >/dev/null 2>&1 && [[ -z "${BAW_CMD:-}" ]]; then
  echo "!! LIVE: 'baw' is not on PATH and BAW_CMD is unset (npm i -g @binance/agentic-wallet@1.10.0, then 'baw' login)" >&2
fi

API_PID=""
UI_PID=""
STOP=0
cleanup() { STOP=1; kill $API_PID $UI_PID 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [[ $BACKEND_ONLY -eq 0 ]]; then
  # real backend unless asked for mock (env beats frontend/.env*, so a copied .env.example can't switch it to mock)
  if [[ $UI_MOCK -eq 1 ]]; then export VITE_API_MODE=mock; else export VITE_API_MODE="${VITE_API_MODE:-http}"; fi
  UI_HOST="${UI_HOST:-127.0.0.1}"
  # npm install every time: picks up new dependencies (e.g. qrcode-generator) after a git pull; fast when up to date
  ( cd frontend && npm install --no-audit --no-fund && npm run dev -- --host "$UI_HOST" ) &
  UI_PID=$!
  echo "==> Regime dashboard: http://localhost:5173  (VITE_API_MODE=$VITE_API_MODE)"
fi

echo "==> Regime backend: TRADING_MODE=$TRADING_MODE  data (saved strategies, agent state, logs): $LOG_DIR  api: http://localhost:$PORT/docs  supervised: $SUPERVISED"
if [[ $SUPERVISED -eq 0 ]]; then
  uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" &
  API_PID=$!
  wait $API_PID
  exit $?
fi

# ---- supervised: restart the API on every exit/crash with exponential backoff (reset after 5 min of uptime)
export LOOP_AUTOSTART="${LOOP_AUTOSTART:-true}"
# config preflight: a refused LIVE start (missing switch/key) is not retried
if ! python -c "from backend.app.settings import Settings; Settings.from_env().validate()"; then
  echo "supervisor: startup refused (config error above) - not starting" | tee -a "$LOG_DIR/supervisor.log"
  exit 3
fi
backoff=2
while [[ $STOP -eq 0 ]]; do
  started=$(date +%s)
  echo "$(date -u +%FT%TZ) supervisor: starting backend" | tee -a "$LOG_DIR/supervisor.log"
  uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" &
  API_PID=$!
  wait $API_PID
  code=$?
  [[ $STOP -eq 1 ]] && break
  up=$(( $(date +%s) - started ))
  echo "$(date -u +%FT%TZ) supervisor: backend exited (code $code, up ${up}s)" | tee -a "$LOG_DIR/supervisor.log"
  if [[ $code -eq 3 && $up -lt 30 ]]; then
    echo "supervisor: startup refused (config error, see above) - not restarting" | tee -a "$LOG_DIR/supervisor.log"
    exit 3
  fi
  (( up > 300 )) && backoff=2
  echo "supervisor: restarting in ${backoff}s" | tee -a "$LOG_DIR/supervisor.log"
  sleep "$backoff"
  backoff=$(( backoff * 2 > 60 ? 60 : backoff * 2 ))
done

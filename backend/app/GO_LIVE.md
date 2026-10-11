# Going LIVE: autonomous 24/7 agent with real funds on BNB Smart Chain

LIVE places **real orders** from a Binance Agentic Wallet (`baw` CLI) on BSC. The default is `TRADING_MODE=SIM`. Every
LIVE switch is opt-in, and the caps default to tiny amounts (25 USDT per order). Read this whole page before switching.

## 1. One-time setup

1. **Install the wallet CLI.** You need Node 18+ and `npm i -g @binance/agentic-wallet@1.10.0`. Check it with `baw --help`.
   - Windows: if `baw` is not on PATH, `run.ps1` sets
     `BAW_CMD=["node","<npm root -g>\@binance\agentic-wallet\dist\index.js"]` automatically. You can also set `BAW_CMD`
     yourself to a JSON argv list, a path to `dist\index.js`, or a binary.
2. **Sign in** (verified by Bot 4 with baw 1.10.0):
   ```bash
   baw auth signin --json                      # shows a QR code and returns a qrCodeId (--image opens the QR as a picture)
   #   -> scan the QR with the Binance app and approve
   baw auth verify --qrCodeId <id> --json      # completes the sign-in
   baw wallet status --json                    # must report CONNECTED
   python -m backend.exchange.baw_smoke        # read-only checks (status, balances, settings, a quote); never swaps
   ```
   The session can expire in **up to 48 h**. The agent then pauses with `session_expired` (alert
   `WALLET_SESSION_EXPIRED`). Repeat these steps, then `POST /api/control/resume`.
3. **Wallet settings in the Binance app** (Agentic Wallet → settings). Set exactly these three:
   - **Daily limit:** the USDT the agent may trade per day.
   - **Trade all tokens:** on, so the wallet doesn't refuse the agent's tokens.
   - **`abnormalTxnHandling = AutoReject`:** if you skip this, a transaction that Binance flags as abnormal waits for
     manual review. While it waits, the wallet is tx-locked and **every** trade is blocked for up to 5 minutes. With
     AutoReject the flagged order fails at once. The agent logs it as a normal failed BUY and carries on.

   The wallet has no per-order limit or token list that can be read or set. The per-order size and the token choice come
   from the agent's own caps (`RISK_LIVE_MAX_ORDER_USDT`, `BINANCE_LIVE_MAX_ORDER_USDT`, `LIVE_ALLOWED_TOKENS`). The agent
   reads the wallet settings via `baw wallet settings` about every 10 minutes and after every trade, and it stays under
   the remaining daily limit.
4. **Fund the wallet.** Deposit USDT for trading plus BNB for gas. Keep at least `RISK_LIVE_MIN_BNB` (default 0.002 BNB)
   above the gas reserve.
   **Wallet funding:** about **70 USDT minimum** (100 USDT is comfortable) and **~0.005 BNB** for gas. Smaller wallets
   can't meet the token minimum order (10 USDT, 20 USDT for Ondo tokens) plus the fee buffer, so every BUY is blocked
   (`/api/wallet` → `limits.min_wallet_usdt` once risk reports it). The agent keeps 0.002 BNB in reserve
   (`BINANCE_BNB_GAS_RESERVE`) and stops with `insufficient_gas` below it.
5. **Daily limit facts:**
   - The **daily quota resets at 00:00 UTC = 05:30 IST**.
   - The `exit_quota_shortfall` warning (alert `LIVE_LIMIT_EXIT_QUOTA_SHORTFALL`) means the remaining quota could not
     cover selling every open position. Raise the quota or reduce exposure.

### Optional: on-chain agent identity (ERC-8004, Bot 5)
This step registers the agent's identity so `/api/system.agent_identity` shows it. It never touches the trading wallet.
1. Create a **new, identity-only** EVM key. Don't use the trading wallet or any wallet holding funds. The command is in
   `backend/agent_studio/README.md`:
   `python -c "from eth_account import Account; a=Account.create(); print(a.address); print(a.key.hex())"`.
   Store the key in your password manager and send about **0.002 BNB** to that address for gas.
2. `pip install -r backend/agent_studio/requirements.txt`
3. Set `AGENT_STUDIO_PRIVATE_KEY=<key>` in the shell only. Never put it in git.
4. Dry run: `python -m backend.agent_studio.register --network mainnet --dry-run --estimate-gas`
5. Register: `python -m backend.agent_studio.register --network mainnet --confirm-mainnet`

`agent_identity` (`erc8004_id`, `registry`, `network`, `explorer_url`, ...) fills in automatically. It stays `null`
until registered.

## 2. Configuration (`.env` at the repo root; git-ignored, never commit it)

```dotenv
TRADING_MODE=LIVE
LIVE_TRADING_ENABLED=true        # switch 1 (app)
BINANCE_LIVE_ENABLED=true        # switch 2 (exchange)
RISK_ALLOW_LIVE=true             # switch 3 (risk layer)
BINANCE_API_KEY=...              # Binance Web3 API (market data, quotes, token status)
BINANCE_API_SECRET=...
BINANCE_WALLET_ADDRESS=0x...     # the Agentic Wallet address (balances, RFQ quotes)

# caps (defaults shown; the wallet's own limits apply on top)
RISK_LIVE_MAX_ORDER_USDT=25
RISK_LIVE_MAX_DAILY_NOTIONAL_USDT=100
RISK_LIVE_MAX_DAILY_LOSS_USDT=25     # reaching it triggers an automatic emergency stop + flatten
RISK_LIVE_MAX_EXPOSURE_USDT=50
RISK_LIVE_MAX_SLIPPAGE_PCT=1.0       # also passed to the executor (baw --slippage), never higher
RISK_LIVE_MAX_DATA_AGE_S=900
RISK_LIVE_MIN_BNB=0.002
BINANCE_LIVE_MAX_ORDER_USDT=25       # Bot 4 executor hard cap (independent second cap)

# 24/7
LOOP_AUTOSTART=true                  # on start: resume the saved agent, else Auto (AUTO_EXECUTE) on DEFAULT_AGENT_SYMBOL
DEFAULT_AGENT_SYMBOL=NVDAB           # bStock, trades 24/7
LOOP_INTERVAL_SECONDS=60
STATE_DIR=./logs                     # agent_state.live.json (agent, open lots, emergency stop) survives restarts
# LIVE_ALLOWED_TOKENS=NVDAB,TSLAon   # optional: restrict the agent's tokens
# BINANCE_ENABLE_XSTOCK=true         # xStock (...x) is off by default (no BSC liquidity)
# BAW_INTENT_FILE=~/.teamwork/baw_intents.json   # order-intent journal (in-flight orders survive restarts)
```

LIVE is **refused at startup** unless `LIVE_TRADING_ENABLED`, `BINANCE_LIVE_ENABLED` and both API keys are set. The error
names each missing item. Without `RISK_ALLOW_LIVE=true` the app starts but every BUY is blocked, and
`/api/live/status.blocked_reasons` says why.

## 3. Start (supervised, restarts on crash)

```bash
backend/app/deploy/run.sh --supervised --backend-only      # macOS / Linux
```
```powershell
.\backend\app\deploy\run.ps1 -Supervised -BackendOnly      # Windows
```

Supervised mode:
- runs a config preflight (it exits 3 instead of looping on a config error);
- sets `LOOP_AUTOSTART=true`;
- restarts the backend whenever it exits, with a 2 → 60 s backoff, logged to `logs/supervisor.log`.

Inside the process, a watchdog restarts a crashed loop task (alert `LOOP_RESTARTED`) and raises `LOOP_STALLED` if no tick
happens for 3 intervals. Drop `--backend-only` / `-BackendOnly` to also run the UI on http://localhost:5173.

**Verify before walking away:**
- `GET /api/live/status` → `can_trade: true`, `blocked_reasons: []`.
- `GET /api/wallet` → `provider: "baw"`, real balances, `limits`.
- `GET /api/loop` → `running: true`, with the heartbeat advancing.

## 4. Operating it

- **Autonomous.** In LIVE the agent executes without asking (`AUTO_EXECUTE`). Confirmation applies only if you turn
  confirmation mode on. Before every BUY it runs two checks:
  1. a risk check with fresh facts (balances, price age, wallet limits, token status, price impact);
  2. a quote for the approved size;
  then it re-checks with that quote, and only then executes.
- **The baw session can silently expire within 48 h.** The agent detects `session_expired` / `wallet_not_ready`, then
  **pauses** and raises the CRITICAL alert `WALLET_SESSION_EXPIRED`. Repeat the sign-in in step 1.2
  (`baw auth signin` → scan → `baw auth verify`), then `POST /api/control/resume`. Re-sign proactively about every
  24–36 h during a long demo.
- **In-flight orders.** A swap whose on-chain outcome is not yet known is `PENDING_CONFIRMATION`, which is not a
  failure. Until it settles:
  - it counts as an open position (no second BUY; counts toward limits);
  - exits aimed at it are held (`pending_exit`), and the executor refuses new swaps (`order_in_flight`).

  The agent reconciles every tick. Once it fills, it is a normal position, and a held emergency stop sells at once.
- **Off-hours / no liquidity.** Exchange codes 40367 / 40369 / 40374 put that token on a 20-minute cooldown, shown in
  decisions and `/api/live/status.cooldowns`.
- **Emergency stop.** `POST /api/control/emergency-stop {"flatten": true}` blocks all BUYs and sells everything. In-flight
  buys are sold the moment they confirm. The stop survives restarts and needs `POST /api/control/reset-emergency-stop`
  to clear.
- **Every trade** shows the tx hash and BscScan link (`explorer_url`), the route, and `real_funds: true`.

## 5. Back to SIM

Stop the process. Then set `TRADING_MODE=SIM`, or remove one of the switches. LIVE state lives in its own file
(`agent_state.live.json`), so SIM never resumes into LIVE and the reverse is also true.

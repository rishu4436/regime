# Demo video script (≤ 4:00)

*Regime, by team `teamwork`, BNB Hack: Tokenized Stocks Edition. Target length 3:45, leaving 15 s of slack.
Record at 1080p, browser zoom 110 %, dark theme. Timestamps are cumulative.*

**First real autonomous mainnet trade (done, 11 Oct 2026)**
At 09:25 IST on 2026-10-11 the agent, in Auto mode running the Grid preset with no human approval, bought
**0.0434 NVDAB for 10 USDT** at about **230.47 USDT** from the Agentic Wallet on BNB Smart Chain mainnet.
Tx: [0x228c0268…a19f22](https://bscscan.com/tx/0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22)
(verified on-chain: status success, block 126950367, from `0xc44eddcffa4227d38bc92a7ca5990953aa7ff0cf`).
Use this tx for the 1:40–1:50 "real fill → BscScan" shot if no newer fill is in the feed.

**Before recording (checklist)**
- Backend running LIVE on Rishu's PC with `baw` signed in (`baw wallet status --json` → `CONNECTED`), and
  `GET /api/live/status` returns `can_trade: true`. See `backend/app/GO_LIVE.md`.
- At least one real NVDAB fill with a tx hash in the activity feed (a few USDT is enough; the track rules say
  "a few dollars is enough"). The first one is above (tx 0x228c0268…a19f22).
- If the wallet session expired, sign in again from the dashboard's **Connect wallet** button (localhost only).
- BscScan tab for that tx open in the background. 8004scan page for the agent open, if registered.
- `abnormalTxnHandling = AutoReject` set in the Binance app (otherwise the wallet can lock mid-demo).
- If any of this isn't ready, use the **fallback path** at the bottom.

---

## 0:00–0:25: The problem

| Shot | Narration |
|---|---|
| Title card: "An autonomous agent for tokenized stocks on BNB Chain". | "Tokenized stocks like NVDAB trade on BNB Chain 24/7, but the stock market they track doesn't. Weekend prices move while the reference price is frozen." |
| Quick cut: a raw `baw` JSON error with code 40367, next to a Binance error-code table. | "Trading them yourself means wallets, gas, session sign-ins and error codes like 40367. An autonomous agent can do it, but only if a normal person can see what it's doing and stop it." |

## 0:25–0:50: One command to start

| Shot | Narration |
|---|---|
| Terminal on the host PC. Type `.\backend\app\deploy\run.ps1 -Supervised` (Windows) or show `backend/app/deploy/run.sh --supervised` (macOS/Linux). | "One command starts it. Supervised mode runs a config preflight, autostarts the agent on NVDAB, and restarts it if it ever crashes." |
| Show the `.env` with the three switches blurred except names: `LIVE_TRADING_ENABLED`, `BINANCE_LIVE_ENABLED`, `RISK_ALLOW_LIVE`. | "Live mode needs three separate switches. Without them it runs in simulation. Secrets stay in a git-ignored `.env`." |
| Browser opens http://localhost:5173. | — |

## 0:50–2:20: Dashboard tour

| Time | Shot | Narration |
|---|---|---|
| 0:50 | **LIVE · MAINNET banner**, zoom in. | "The first thing you see is a red banner that never goes away: real funds, BNB Smart Chain mainnet, this wallet, and the hard caps in dollars." |
| 1:00 | **Autonomy monitor**: "Agent running autonomously", running since, last tick, next tick. Wait for the last-tick counter to reset. | "Is it alive? This heartbeat answers that. Last tick a few seconds ago, next one in under a minute. If the loop stalls, this card turns red." |
| 1:15 | **Wallet panel**: address → BscScan, "Binance Agentic Wallet (baw)", chain 56, USDT and BNB balances. | "It trades from a Binance Agentic Wallet. Our code never handles a trading private key; the wallet signs every swap." |
| 1:25 | **Caps panel**: per-order, daily traded, daily loss, open exposure meters; point at the "set by agent" / "set by wallet" pills. | "Two layers of limits. Our risk layer's caps, and the limits Rishu set in the Binance app. The agent obeys whichever is stricter, and the dashboard tells you which one is in charge." |
| 1:40 | **Activity feed**, filter "trades & blocks". Click a BUY row with a real tx. | "Every decision gets a plain-English explanation from Grok, with a template fallback. Here's a buy: route, price, fee, price impact…" |
| 1:50 | Click **tx ↗** → BscScan tab with the swap. | "…and the transaction on BscScan. Nothing to take on trust." |
| 2:00 | Back to the feed: a **risk block** or stay-out in plain English, e.g. a red chip "Ondo market not tradable now (off-hours)" or "Below Ondo 20 USD minimum order", or a failed risk check chip. | "And when it doesn't trade, it says why, in words, not error 40367. Here the Ondo market is in off-hours, so it stays out and cools that token down for 20 minutes." |
| 2:10 | Token status badges (Trading / Off-hours · opens Mon / No liquidity) if present. | "Each token family behaves differently: bStocks trade 24/7, Ondo has off-hours sessions, xStock has no liquidity on BSC right now, so we exclude it." |

## 2:20–2:50: Kill switch

| Shot | Narration |
|---|---|
| Click **❚❚ Pause** → monitor turns amber "Paused by kill switch". | "One click pauses it." |
| Click **■ Emergency stop** → confirm dialog → banner "Emergency stopped". | "Emergency stop blocks every new buy, survives restarts, and closes open positions. A daily loss cap triggers the same stop automatically." |
| Click **↺ Reset e-stop**, then **▶ Resume**. Monitor back to green. | "Resetting it is a deliberate second step." |

## 2:50–3:15: Agent identity (BNB Agent Studio)

| Shot | Narration |
|---|---|
| **Agent identity card**: ERC-8004 id, network, registry, "Executes with Agentic Wallet". | "The agent has an on-chain identity, registered with BNB Agent Studio's ERC-8004 registry, using a separate identity-only key that never holds trading funds." |
| Click the id → 8004scan agent page. | "Anyone can look it up, and see which wallet it trades from." |
| *(If not registered yet: the card reads "not registered yet"; say "registration is a one-command step in GO_LIVE.md" and skip the 8004scan cut.)* | |

## 3:15–3:45: Why it matters

| Shot | Narration |
|---|---|
| Slow pan over the whole dashboard. | "A non-crypto user doesn't want to learn gas, quotes or session QR codes. They want four answers: is it using real money, is it alive, why did it trade, and what's the most it can lose." |
| Session-expired banner (trigger in mock if needed). | "Even the wallet signing out is a sentence, with the fix." |
| End card: repo URL, "built on Binance Web3 API · Agentic Wallet · BNB Agent Studio · Grok". | "That's what brings people on-chain: an agent you can read, check on BscScan, and stop with one click." |

---

## Fallback: mock "Demo LIVE scenario" (no funds, no backend)

Use this if the real trade, the `baw` session or the region-gated Web3 API isn't available at recording time.

```bash
backend/app/deploy/run.sh --mock              # or: cd frontend && npm install && npm run dev (mock is the npm default)
```

1. Turn on the **Demo LIVE scenario** toggle (shown only with the mock API; the page reloads).
2. Everything above works the same: LIVE banner, autonomy monitor on NVDAB in AUTO_EXECUTE, wallet panel
   with demo balances (BNB drains on each fill, so the low-gas warning appears), caps with an exit-reserve warning,
   a settling buy ("⏳ waiting on chain" / "Exit queued until fill"), kill switches.
3. **Say it on camera:** "This is our mock LIVE scenario. The banner shows *demo · no funds* and tx links are marked
   *(demo)*, they are not on chain." Don't click a demo tx link through to BscScan; it won't resolve.
4. If a real tx exists from any earlier run, cut to its BscScan page separately and say so.

## Shots for the special-prize judges

| Prize | Shots to watch |
|---|---|
| **Best Use of Agentic Wallet / Wallet Skills** | 0:25 (three LIVE switches), 1:15 wallet panel, 1:25 caps "set by wallet", 1:40–1:50 real fill → BscScan, 2:00 Binance error code turned into a stay-out, 3:15 session-expired banner. |
| **Best Use of BNB Agent Studio** | 2:50–3:15 identity card and 8004scan page; the "Executes with Agentic Wallet" link between identity and wallet. |

## Facts to re-check right before recording

- `run.ps1`, `GO_LIVE.md`, `/api/tokens.token_status` and `cooldowns` are merged; the dashboard **Connect wallet** flow
  (API v0.8) comes with bot6/polish, so confirm it is merged before showing it.
- Emergency stop from the UI sends `flatten: false`: positions close **on the next bar** at that bar's close, not instantly.
  With 1 h bars, say "closes open positions" without "immediately", or demo with no open position.
- Whether the ERC-8004 mainnet registration has been run (`ai-stack.md` §4 says it still has to be run from Rishu's PC).

# Regime demo script (3:00)

*Regime, BNB Hack: Tokenized Stocks Edition. Repo: https://github.com/rishu4436/regime*

Total length is 3:00. Timestamps are cumulative. Record at 1080p, browser zoom 110%, dark theme.
Only show what is on the screen. If something isn't there on the day, skip it and say so.

## Real mainnet trades (both verified on BscScan: status success, wallet `0xc44eddcffa4227d38bc92a7ca5990953aa7ff0cf`)

| When (11 Oct 2026, IST) | What | Tx |
|---|---|---|
| 09:25 | First autonomous buy (Auto mode, Grid): 10 USDT of NVDAB at about 230.47 USDT | [0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22](https://bscscan.com/tx/0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22) |
| 12:01 | Autonomous buy in Auto mode, Grid strategy: 10 USDT of NVDAB | [0x187fdc3e1023e1ecd37af1dc7bed4176a13e75e39a0b35727681538c0690bf7f](https://bscscan.com/tx/0x187fdc3e1023e1ecd37af1dc7bed4176a13e75e39a0b35727681538c0690bf7f) |

## Before recording (checklist)

- [ ] Backend running in **LIVE** mode on Rishu's PC. The red **LIVE · MAINNET** tag shows at the top of the dashboard.
- [ ] Dashboard started with `VITE_API_MODE=http`, so it shows the real backend and not mock data.
  (The one-command `run.sh` / `run.ps1` does this. If the **Demo LIVE scenario** toggle is visible, you are on mock data.)
- [ ] Wallet connected. If the top bar shows **Reconnect wallet**, click it and scan the QR in the Binance App.
  When it's connected, the top bar shows only the wallet address.
- [ ] The agent is running **Auto** mode with **Grid** on **NVDAB**, and the two NVDAB lots show under **Open positions**.
- [ ] Two BscScan tabs open in the background, one for each tx above.
- [ ] Trading limits panel visible on the dashboard (it is on main since PR #23).
- [ ] Decide how to show Emergency stop (see 2:05). In LIVE it sells the open lots on the next bar.

---

## 0:00–0:15: What Regime is

| Shot | Say |
|---|---|
| Dashboard, full screen. | "This is Regime. It's an autonomous agent that trades tokenized stocks on BNB Chain. It trades through the Binance Agentic Wallet and gets its market data from the Binance Web3 API." |

## 0:15–0:45: The dashboard

| Time | Shot | Say |
|---|---|---|
| 0:15 | Point at the red **LIVE · MAINNET** tag and banner. | "This is live. Real money, on BNB Smart Chain mainnet." |
| 0:22 | **Agent** card: market regime, active strategy (Grid), token NVDAB. | "Regime reads the market first. Right now NVDAB is in this regime, so Regime picked one strategy for it: Grid. It runs one strategy at a time." |
| 0:32 | Autonomy monitor: running since, last tick, next tick. | "This shows it's alive. It checks the market every tick, around the clock." |
| 0:38 | **Open positions**: the two NVDAB lots. | "These are the two NVDAB positions it holds now." |

## 0:45–1:10: A decision and its reason

| Time | Shot | Say |
|---|---|---|
| 0:45 | **Last decision** card, then a row in the **Activity** feed. Open the reason. | "Every decision comes with a reason in plain English. Buy, sell or stay out. It names the regime, the strategy and the numbers behind it." |
| 0:58 | Keep the reason on screen. | "The explainer writes this. It uses Grok if a key is set, and built-in templates if not, so it works without any AI key. It only explains. It can never place a trade." |

## 1:10–1:40: Real trades on BscScan

| Time | Shot | Say |
|---|---|---|
| 1:10 | In the Activity feed, click **tx ↗** on the 12:01 buy, or switch to its BscScan tab. | "Here's a trade Regime made on its own today at 12:01, in Auto mode with Grid. 10 USDT of NVDAB." |
| 1:22 | BscScan page: status Success, from the wallet address. | "It's on BscScan. Status success, from our Agentic Wallet. Anyone can check it." |
| 1:32 | Switch to the 09:25 BscScan tab. | "And this was Regime's first autonomous trade, at 09:25 this morning." |

**If there's no new trade during the demo:** that's normal. Regime often stays out. Say "No new trade right now, so here are the ones it already made," and show the two recorded txs above.

## 1:40–2:05: Trading limits

| Time | Shot | Say |
|---|---|---|
| 1:40 | **Trading limits** card. Click through **Order size**: Strategy decides, Fixed USDT, % of wallet. | "You set how much Regime may trade. Let the strategy decide the size, use a fixed amount in USDT, or a percent of the wallet." |
| 1:50 | Point at Max per order, Max total invested, Max in one token, Daily loss limit, Daily buy limit. | "Then hard caps: per order, in total, per token, daily loss and daily buys. You can change them while it runs. The wallet's own daily limit in the Binance App stays the final cap." |
| 1:58 | Optional: set Fixed USDT above Max per order, save, show the red message, then click **Undo changes**. | "A bad value gets a plain sentence, not an error code, and nothing changes." |

## 2:05–2:25: Safety

| Time | Shot | Say |
|---|---|---|
| 2:05 | A stay-out or a blocked risk check in the Activity feed, with its reason. | "Every trade goes through the risk layer first. It can block any trade, and it always says why." |
| 2:12 | Click **❚❚ Pause**, show "Paused", then click **▶ Resume**. | "One click pauses it." |
| 2:18 | Click **■ Emergency stop**. Show the confirm dialog, then click **Cancel**. | "Emergency stop blocks every new buy and closes open positions. It stays on until you reset it." |

**Emergency stop in LIVE:** confirming it sells the open NVDAB lots on the next bar. Only confirm if Rishu wants that. Otherwise show the dialog and cancel, as above.

## 2:25–2:45: Agent Studio identity

| Time | Shot | Say |
|---|---|---|
| 2:25 | **Agent identity** card (BNB Agent Studio · ERC-8004). Show the three steps: create identity wallet, fund it with BNB for gas, register. | "Regime is ready for an on-chain identity with BNB Agent Studio, using ERC-8004. It uses a separate small wallet that never touches trading funds." |
| 2:37 | Card reads "not registered yet". | "It's ready, but not registered yet." |

<!-- AGENT STUDIO REGISTRATION: if Rishu registers before recording, replace the 2:37 row with:
| 2:37 | Card shows the ERC-8004 id and network. Open the registration tx on BscScan. | "It's registered. Here's its ERC-8004 id, and the registration tx on BscScan." |
Registration tx: https://bscscan.com/tx/<REGISTRATION_TX_HASH>
ERC-8004 id: <ID>
-->

## 2:45–2:55: Manual Strategy Builder

| Time | Shot | Say |
|---|---|---|
| 2:45 | **Manual Builder** tab. Scroll past the entry rules (RSI, SMA, EMA, MACD, Bollinger, volume, breakout, AND/OR) and exits (take-profit, stop-loss, trailing). | "If you want your own rules, build them here without code, backtest them, then run them live with the same engine." |

## 2:55–3:00: Close

| Shot | Say |
|---|---|
| Back to the dashboard. Repo link on screen: https://github.com/rishu4436/regime | "That's Regime. It trades by itself, explains every move, and stays inside your limits." |

<!-- WATCHLIST: uncomment if merged by 13:45
Time budget: 15 s. Take it from 2:25–2:45 (Agent Studio, cut to 10 s) and 2:45–2:55 (Manual Builder, cut to 5 s).

## Watchlist and Auto-pick (15 s)

| Shot | Say |
|---|---|
| **Auto Mode** tab: the watchlist picker. Search the full token list (about 680 tokenized stocks) and add tokens, up to 5. | "Regime can watch up to five tokens at once. Pick them from about 680 tokenized stocks." |
| Turn on **Auto-pick** and choose a narrative, such as AI & chips. Show the picks. | "Or let Auto-pick choose them by theme, using momentum, trend and volume. The risk caps cover all of them together." |
| Dashboard **Watchlist** card. | "It checks each one every tick." |
-->

---

## Facts to re-check right before recording

- Mode: both trades are Auto mode with Grid, made by Regime on its own (confirmed from the PC decision log).
- Agent Studio: still not registered unless Rishu registers. If he does, fill in the placeholder above.
- Watchlist: the picker is in the frontend, but the backend (PR #28) isn't merged yet. Keep that section commented
  out unless it merges by 13:45.

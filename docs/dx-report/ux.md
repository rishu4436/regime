# Product & UX: making an autonomous trading agent legible

> **TODO (Rishu): add your own experience here before submitting.** The judges reject "perfunctory or AI-generated"
> reports, so this section needs your own voice. Suggested prompts: the moment you first scanned the `baw` QR on your
> PC; what confused you in the Binance app's Agentic Wallet settings; what it felt like to watch the agent trade real
> USDT unattended; anything below that you disagree with. Replace or annotate any line that doesn't match what you saw.

*Part of the Developer Experience Report for team `teamwork` (BNB Hack: Tokenized Stocks). Written 11 Oct 2026.
Everything here points at code on `bot1/live` (PR #8), `backend/app/API.md` v0.7, `backend/app/GO_LIVE.md`, or our
research notes (the exchange/wallet team's Agentic Wallet deep research, the strategy team's skills research). The AI layer (Grok explanations, reason
vocabulary, Agent Studio identity) is covered in [`ai-stack.md`](ai-stack.md); we don't repeat it here.
Items marked **[unverified]** were not confirmed against a real mainnet run.*

## 1. The product question we designed for

Judges ask: *would it bring non-crypto-native users on-chain?* Our answer is that a user who has never used a DEX
should be able to open one page and answer four questions without reading logs:

1. **Is it trading with real money right now?**
2. **Is it actually alive?**
3. **Why did it (not) trade?**
4. **What is the most it can lose, and who set that limit?**

The dashboard (`frontend/src/components/Live.tsx`, `frontend/src/lib/live.tsx`) is built around those four questions.

## 2. How the dashboard answers them

| Question | UI element | What it does (code) |
|---|---|---|
| Real money? | **LIVE · MAINNET banner** (`LiveBanner`) | Red, sticky, non-dismissible on every tab whenever `/api/system.trading_mode == "LIVE"`. Says in one sentence: real funds, BNB Smart Chain mainnet, from wallet `0x…` (linked to BscScan), "without asking, inside hard caps", followed by the caps in dollars (per order, daily loss stop, max open). If the live gate is closed it adds "New trades blocked:" with the backend's reasons. |
| Alive? | **Autonomy monitor + heartbeat** (`AutonomyMonitor`) | One status line in plain words ("Agent running autonomously", "Paused by kill switch", "Auto-stopped: daily loss cap hit", "Heartbeat lost: loop not ticking"). Below it: running since, last tick ("12s ago"), next tick ("in 41s"), tick and restart counts. The page re-polls `/api/loop` every 5 s on its own, so "last tick" keeps aging even if no events arrive, and the monitor turns red after 3 missed intervals (min 15 s). |
| Can I stop it? | **Kill switches** on the same card | Pause / Resume, Emergency stop (confirm dialog explains what happens to open positions) and Reset e-stop. An automatic e-stop from the daily loss cap is labelled as such, not as a generic error. |
| Why? | **Activity feed** (`ActivityFeed`) | Every decision, including STAY_OUT, shows the explanation sentence, the raw reason underneath, failed risk checks as red chips with their detail, and "✓ N risk checks passed". A "trades & blocks" filter hides the routine stay-outs. Binance error codes in a reason (40366/67/69/74/75) get a human chip, e.g. "Below Ondo 20 USD minimum order" (`exchangeCodeLabels`, `lib/strategy.ts`). |
| Did it really happen? | **BscScan link per trade** (`TxLink`) | `tx 0x12ab…cdef ↗` opens `explorer_url` (or `https://bscscan.com/tx/<hash>`). Simulated hashes (`0xsim…`) are never linked, and demo-mode hashes are labelled "(demo)". Each fill also shows route, notional, avg price, fee, price impact (red at ≥ 1 %) and a "real funds" pill. |
| Max loss, and who set it? | **Caps panel** (`CapsPanel`) | Meters for per order, daily traded, daily loss (distance to auto e-stop) and open exposure. Each cap carries a **"set by agent" / "set by wallet"** pill from `limit_source`, and shows "agent $25 vs wallet $X" when both exist. Wallet scope is split into **Tradable tokens** (what the agent may trade) and **Allowed by wallet** ("All tokens" / "Limited by wallet"). |
| Wallet health? | **Wallet panel** (`WalletPanel`) | Address (BscScan + copy), "Binance Agentic Wallet (baw)", chain 56 (mainnet), balances with USD value, and an amber/red **gas banner** when BNB drops below 2× / 1× the reserve ("New buys are blocked until you top up"). |
| Why did it stop overnight? | **Session-expired banner** (`SessionBanner`) | Red banner: "Agentic Wallet session expired, re-scan QR on host PC", with the exact commands (`baw auth signin`, scan in the Binance App). Triggered by `wallet.session_expired` or a `session_expired` / `wallet_not_ready` blocked reason. |
| Is my order stuck? | **Settling-on-chain states** | A swap whose outcome isn't known yet shows "⏳ waiting on chain", never "bought" or "failed". The position row is marked as settling and its Close button is disabled ("Buy still settling on-chain: exit queued until fill"). A held exit shows **"Exit queued until fill"**, and `order_in_flight` shows "Earlier swap still settling, waiting". |
| Which kind of stock token? | **Family + status badges** | bStock / Ondo / xStock badge from the symbol suffix, and a token status pill (Trading / Off-hours · opens Mon 05:35 / Closed / No liquidity · min 20 USDT) from `/api/tokens.token_status`. Hidden when the backend doesn't send it. |

Design rules we followed throughout:
- **Never show a blank.** Anything the backend doesn't expose renders as "not available", and derived values say so
  (e.g. the live gate notes "inferred from /api/system", heartbeat notes "seen by this page").
- **Autonomous by default, and say so.** In LIVE the agent runs `AUTO_EXECUTE`; the monitor reads "autonomous / real
  funds" and only says "asks before entries" if someone turns confirmation mode on.
- **Default to the token that actually trades 24/7.** NVDAB (bStock) is the default symbol; xStock is greyed out
  outside SIM (see pain point 3).
- **A demo that can't lie.** The mock "Demo LIVE scenario" toggle (`api/mock.ts`) reproduces the whole LIVE UI in the
  browser, with a "demo · no funds" pill on the banner and "(demo)" on every tx link. Nothing leaves the browser.

## 3. UX pain points caused by the Binance APIs and docs

Each of these forced UI or backend work that a non-crypto user would otherwise feel as confusion. Overlaps with
`ai-stack.md` §5 are cross-referenced instead of repeated.

| # | Pain point | What the user would have seen | What we did | Suggested fix |
|---|---|---|---|---|
| 1 | **Per-token tradability has to be inferred.** Session info lives in the keyless `rwa/asset/market/status`, the `offhours` block is undocumented (strategy research §6), and the real answer only comes from a quote error. | A "Buy" signal on a token that can't be bought right now, then a failure. | Backend calls quote before every BUY; an unavailable quote is a STAY_OUT, not a failed trade, and the token cools down 20 min (`/api/live/status.cooldowns`). UI shows a status badge per token. | One documented per-token field: `tradable_now`, `session`, `next_open_time`, `min_order_usd`, `liquidity_ok`. |
| 2 | **Ondo off-hours candles are zero-volume** (~0.2 % hourly range on Sun 11 Oct, strategy research §3 [L]). Ondo K-line volume isn't DEX volume, and `rwa/dynamic` volume is *US stock* volume (strategy research §2.4). | A flat chart that looks broken, and volume rules that silently block every Ondo buy. | NVDAB is the default chart and agent token; the Ondo status badge says "Off-hours · opens …". | Mark off-hours bars (or return `null` volume) and document which volume field means what. |
| 3 | **xStock reports TRADING but has no liquidity.** TSLAx `openState=true, TRADING`, liquidity `0`, last bars 15 Aug → 4 Oct, and a Sunday quote returned **40374** (exchange/wallet research §5, strategy research §3). | A green "Trading" token that never fills. | xStock is off by default in LIVE (`BINANCE_ENABLE_XSTOCK`), greyed out in the token picker, and labelled "xStock excluded (no liquidity on BNB Chain)". | Include a liquidity flag in market status (also `ai-stack.md` #11). |
| 4 | **Numeric error codes with no human message.** 40366 / 40367 / 40369 / 40374 / 40375 (and 40304 returned with HTTP 200) have to be mapped by hand from the docs. | "Error 40367" in an activity feed. | A shared code → sentence table in the UI (`CONDITION_LABEL`) and in the explainer (`backend/llm/live.py`), e.g. "Ondo market not tradable now (off-hours)". | Return a `message` and a `retryable` / `retry_after` field with every code. |
| 5 | **Ondo's 20 USD minimum is discoverable only via error 40375.** With our 25 USDT per-order cap and a 0.25 live size factor, Ondo buys fall below it (strategy research §3, item 4). | Repeated "failed" buys for no visible reason. | Labelled "Below Ondo 20 USD minimum order"; the status badge shows "min 20 USDT" when `token_status.min_order_usdt` is present. | Expose min/max order size per token in the token list or status endpoint. |
| 6 | **Wallet limits are only partly readable.** There is no per-transaction cap at all, and the allowed-token list can't be read back, only the boolean `tradeAllTokens` (exchange/wallet research §3). Whether sells use the daily quota is undocumented **[unverified]**. | "Why was this token rejected?" with no way to show the user their own wallet setting. | Caps marked "set by agent" vs "set by wallet"; per-order cap is always ours; wallet scope shown as "Limited by wallet" rather than a fake list; exit-reserve warning when the remaining quota can't cover selling every position. | Readable allowlist and an optional per-tx cap in `baw wallet settings`; document whether sells count (`ai-stack.md` #4, #6). |
| 7 | **`baw market-order quote` has no price impact**, reference price or quoteId (exchange/wallet research §6). | No way to say "this order would move the price 1.3 %" before it executes. | Price impact comes from the separate Web3 `/aggregator/quote`; the feed shows it per fill, or "Price impact unknown" (`ai-stack.md` #5). | Return `priceImpactPercent` and fill amounts in the CLI JSON. |
| 8 | **The QR session expires silently** (max 48 h, plus inactivity sign-out). There is no warning before expiry (exchange/wallet research §8.2). | A "24/7" agent that quietly stopped trading overnight. | Backend pauses with `WALLET_SESSION_EXPIRED`; the UI shows a red banner with the exact re-sign-in steps. GO_LIVE.md tells the operator to re-sign every 24–36 h. | An expiry warning event (e.g. 2 h before), and a long-lived, revocable agent credential (`ai-stack.md` #2). |
| 9 | **`abnormalTxnHandling=NeedConfirmation` locks the wallet** for up to 5 min, including stop-loss sells, and the docs recommend it (exchange/wallet research §3). | "Your agent is autonomous" but a phone tap is needed, and exits are frozen. | GO_LIVE.md step 3 makes AutoReject a required setup step. | Default agent wallets to AutoReject, or never lock exits behind a review (`ai-stack.md` #3). |
| 10 | **Swaps can be in flight with unknown outcome**, and `market-order list` has no received amount (exchange/wallet research §6–7). | A buy that is neither filled nor failed. | Explicit "waiting on chain" / "Exit queued until fill" states instead of guessing; real fill read later from `tx-history`. | A terminal status plus received amount on the order itself. |

## 4. What helped

- The keyless RWA market-status endpoint made a per-token status badge cheap, once we knew it existed.
- Every execution carrying a tx hash made the "Did it really happen?" link trivial; BscScan is the one on-chain page a
  newcomer can trust without learning anything.
- The `baw` JSON error codes (`SESSION_EXPIRED`, `TX_LOCKED`, `APP_CONFIRMATION_REQUIRED`) were specific enough to
  turn into plain-English banners.

## 5. Not yet verified on mainnet (as of writing)

- **[unverified]** "Settling on-chain" and "Exit queued until fill" were only seen in the mock LIVE scenario; no real
  position had opened during our test run of `integ-v8` on NVDAB.
- **[unverified]** Token status badges depend on `/api/tokens.token_status`, which lands with the app/integration layer's live PR.
- **[unverified]** The session-expired banner was tested against mocked reasons, not a real 48 h expiry.
- **TODO (Rishu):** add the first real mainnet tx hash here once the live trade is done.

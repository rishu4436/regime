# Developer Experience Report: Regime

*BNB Hack: Tokenized Stocks Edition. Repo: [rishu4436/regime](https://github.com/rishu4436/regime). Written 11 Oct 2026.*

## Summary

**Regime** is an autonomous trading agent for tokenized stocks (bStocks such as NVDAB, plus Ondo tokens) on BNB Smart
Chain mainnet, built to run 24/7 without asking before each trade. It reads market data, quotes and token status from
the **Binance Web3 API**, and it executes trades through the **Binance Agentic Wallet** (`baw` CLI). A deterministic
strategy/regime engine proposes every trade, and a deterministic risk layer can block or shrink it. **The LLM (xAI
Grok) only explains decisions in plain language; it never places trades.** Regime's **ERC-8004 identity** is prepared
with the BNB Agent Studio SDK (`bnbagent` 0.5.0) but not registered on-chain yet.

**Status at the time of writing:** the full loop has run end to end in simulation and against fake exchanges, and
**Regime made its first real-funds trade on BNB Chain mainnet on its own** on 11 Oct 2026 at 09:25 IST: Auto mode
(Grid) bought 0.0434 NVDAB for 10 USDT at about 230.47
([BscScan](https://bscscan.com/tx/0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22)).
**The ERC-8004 identity has not been registered on mainnet yet.** Every section marks what it did not verify.

## Sections

| Section | What it covers |
|---|---|
| [AI stack](ai-stack.md) | How the engine, risk, executor and LLM fit together; Grok with template fallback and LIVE guardrails; the shared reason vocabulary; Agent Studio ERC-8004 identity; the x402 idea |
| [Binance Web3 API and Agentic Wallet](exchange.md) | What we built on the Web3 API and `baw`, and 14 pain points with our workarounds and suggested fixes |
| [Strategy engine](strategy.md) | Indicators, regimes, presets, backtester, bugs fixed relative to the reference simulator, and what real mainnet candles broke |
| [Risk control](risk.md) | The LIVE risk profile, caps vs hard gates, safety layers under the wallet's own limits, pending orders, risk-side pain points |
| [Integration and monitoring](integration.md) | The trading loop, in-flight order handling, SIM/PAPER/LIVE separation, e2e tests, run scripts |
| [Product and UX](ux.md) | How the dashboard shows whether it is live, whether it is alive, why it (didn't) trade, and who set each limit |
| [Demo video script](../demo-script.md) | The 4-minute demo walkthrough |

## AI stack

The required **AI stack** section is [ai-stack.md](ai-stack.md). In short:
- Deterministic engine and risk decide.
- Grok (`XAI_API_KEY`) explains, with an offline template fallback.
- In LIVE, explanations must not use approval wording and must carry a BscScan tx link.
- The Agentic Wallet executes under both its own limits and ours.
- Agent Studio is used for Regime's ERC-8004 identity (agent name `Regime`); registration is prepared, not done yet.

## Top pain points

1. **Region block with HTTP 200.** From our US-hosted build box, every Web3 API call returned 40304, so every live check had to run from the team lead's PC in India. There is no testnet. See [exchange.md](exchange.md) #1–#2.
2. **QR-only `baw` sign-in, and the session can end silently** (up to 48 h, plus inactivity sign-out), so a "24/7" agent still needs a human. See [exchange.md](exchange.md) #9.
3. **`abnormalTxnHandling=NeedConfirmation` tx-locks the wallet,** blocking stop-loss sells too, until it's set to AutoReject. See [exchange.md](exchange.md) #11.
4. **Wallet limits are only partly readable.** There's no per-tx cap and no readable allowlist, and it's undocumented whether sells use the daily quota. See [exchange.md](exchange.md) #10 and [risk.md](risk.md) #1–#2.
5. **"TRADING" is not the same as liquid.** xStock showed TRADING but quoted 40374. Ondo off-hours candles have zero volume. See [exchange.md](exchange.md) #4 and [strategy.md](strategy.md) #1.
6. **Agent Studio's managed runtime is testnet-only and has no Agentic Wallet wallet kind,** so a mainnet trading agent can use Studio only for identity. See [ai-stack.md](ai-stack.md) #8.
7. **Numeric error codes with no message, and no shared reason vocabulary.** Every layer had to map codes to plain English by hand. See [ai-stack.md](ai-stack.md) #12 and [ux.md](ux.md) #4.

## What the 11 Oct mainnet run recorded

These are the facts already in this repo. The first-person account of setting up the Binance app, scanning the QR, and what to tell the Binance and BNB Chain teams is still the author's to write. Judges reject a report that pretends that account was written by the team when it was not.

- First autonomous buy: 11 Oct 2026, 09:25 IST, 10 USDT of NVDAB, Auto mode, Grid. [BscScan](https://bscscan.com/tx/0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22).
- Second autonomous buy: the same day, 12:01 IST, again 10 USDT of NVDAB. [BscScan](https://bscscan.com/tx/0x187fdc3e1023e1ecd37af1dc7bed4176a13e75e39a0b35727681538c0690bf7f).
- The `baw` session dropped the same day after about 80 minutes, not after the 48-hour ceiling. `GO_LIVE.md` records `WALLET_SESSION_EXPIRED` and a wallet status of "illegal parameter". Stop-loss and take-profit swaps wait until sign-in.
- The ERC-8004 identity is prepared in `backend/agent_studio/` and is not registered on-chain in this tree. The registration file now includes `active: true`, `x402Support: false`, and a public image, because Regime does not charge callers via x402.
- Wallet settings still do not return a per-transaction cap or the token allow-list. A LIVE buy is now blocked when `tradeAllTokens` is false and no list came back, and when the wallet-limits snapshot or the token-status snapshot is missing.

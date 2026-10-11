# Developer Experience Report: team `teamwork`

*BNB Hack: Tokenized Stocks Edition. Repo: [rishu4436/teamwork](https://github.com/rishu4436/teamwork). Written 11 Oct 2026.*

## Summary

`teamwork` is an autonomous trading agent for tokenized stocks (bStocks such as NVDAB, plus Ondo tokens) on BNB Smart
Chain mainnet, built to run 24/7 without asking before each trade. It reads market data, quotes and token status from
the **Binance Web3 API**, and it executes trades through the **Binance Agentic Wallet** (`baw` CLI). A deterministic
strategy/regime engine proposes every trade, and a deterministic risk layer can block or shrink it. **The LLM (xAI
Grok) only explains decisions in plain language; it never places trades.** The agent also has an on-chain **ERC-8004
identity** built with the BNB Agent Studio SDK (`bnbagent` 0.5.0).

**Status at the time of writing:** the full loop has run end to end in simulation and against fake exchanges. **No
real-funds swap has been made yet**, and **the ERC-8004 identity has not been registered on mainnet yet.** Statements
about live fills come from the `baw` CLI source and docs, and every section marks what it did not verify.

## Sections

| Section | What it covers |
|---|---|
| [AI stack](ai-stack.md) | How the engine, risk, executor and LLM fit together; Grok with template fallback and LIVE guardrails; the shared reason vocabulary; Agent Studio ERC-8004 identity; the x402 idea |
| [Binance Web3 API and Agentic Wallet](exchange.md) | What we built on the Web3 API and `baw`, and 14 pain points with our workarounds and suggested fixes |
| [Strategy engine](strategy.md) | Indicators, regimes, presets, backtester, bugs fixed relative to the reference simulator, and what real mainnet candles broke |
| [Risk control](risk.md) | The LIVE risk profile, caps vs hard gates, safety layers under the wallet's own limits, pending orders, risk-side pain points |
| [Integration and monitoring](integration.md) | The trading loop, in-flight order handling, SIM/PAPER/LIVE separation, e2e tests, run scripts *(being written)* |
| [Product and UX](ux.md) | How the dashboard shows whether it is live, whether it is alive, why it (didn't) trade, and who set each limit |
| [Demo video script](../demo-script.md) | The 4-minute demo walkthrough |

## AI stack

The required **AI stack** section is [ai-stack.md](ai-stack.md). In short:
- Deterministic engine and risk decide.
- Grok (`XAI_API_KEY`) explains, with an offline template fallback.
- In LIVE, explanations must not use approval wording and must carry a BscScan tx link.
- The Agentic Wallet executes under both its own limits and ours.
- Agent Studio provides the agent's ERC-8004 identity.

## Top pain points

1. **Region block with HTTP 200.** From our US-hosted build box, every Web3 API call returned 40304, so every live check had to run from the team lead's PC in India. There is no testnet. See [exchange.md](exchange.md) #1–#2.
2. **QR-only `baw` sign-in, and the session can end silently** (up to 48 h, plus inactivity sign-out), so a "24/7" agent still needs a human. See [exchange.md](exchange.md) #9.
3. **`abnormalTxnHandling=NeedConfirmation` tx-locks the wallet,** blocking stop-loss sells too, until it's set to AutoReject. See [exchange.md](exchange.md) #11.
4. **Wallet limits are only partly readable.** There's no per-tx cap and no readable allowlist, and it's undocumented whether sells use the daily quota. See [exchange.md](exchange.md) #10 and [risk.md](risk.md) #1–#2.
5. **"TRADING" is not the same as liquid.** xStock showed TRADING but quoted 40374. Ondo off-hours candles have zero volume. See [exchange.md](exchange.md) #4 and [strategy.md](strategy.md) #1.
6. **Agent Studio's managed runtime is testnet-only and has no Agentic Wallet wallet kind,** so a mainnet trading agent can use Studio only for identity. See [ai-stack.md](ai-stack.md) #8.
7. **Numeric error codes with no message, and no shared reason vocabulary.** Every layer had to map codes to plain English by hand. See [ai-stack.md](ai-stack.md) #12 and [ux.md](ux.md) #4.

## Rishu's experience (to fill in)

> **TODO (Rishu):** write this in your own words before submitting. Judges reject reports that read as perfunctory or
> AI-generated. Suggested prompts:
> - setting up the Binance app and Agentic Wallet;
> - scanning the `baw` QR on your PC;
> - finding (or not finding) the wallet's limit settings;
> - the first real trade and its BscScan link;
> - registering the agent identity;
> - what you would tell the Binance and BNB Chain teams to fix first.

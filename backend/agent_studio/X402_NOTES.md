# Notes: paying BNB Chain's Stock Analyze Agent with `baw x402-payment` (no code here)

Status: **notes only**. Nothing in `backend/agent_studio` spends money or calls this service.
Source: Bot 4's research (`agentic-wallet-deep-research.md` §2, §4b, from the baw 1.10.0 CLI help and the
Agent Studio campaign docs). Not exercised end to end by Bot 5.

- Service: `https://stock-agent.bnbchain.org`. Price is about **0.1 U per call** (check `GET /x402/price` first).
  Limit: 30 calls per wallet per rolling hour. A report takes 2–5 minutes, so it is a slow "second opinion",
  never on the trading hot path.
- Payment comes from the Agentic Wallet's own **x402 quota ($20/day, fixed, separate from the trading `dailyLimit`)**.
  `baw wallet settings --json` shows `x402DailyLimit`, `x402QuotaUsed`, `x402QuotaLeft`.

Flow (manual, on Rishu's PC, signed in to `baw`):

1. `curl https://stock-agent.bnbchain.org/x402/price`
2. `POST /x402/analyze/async` with the request body. The response is **HTTP 402** with the payment requirements.
3. `baw x402-payment preview --paymentRequirements '<json or base64 from the 402>' --json` -> returns a `paymentId`
   and the payment options. Check the amount (about 0.1) before going on.
4. `baw x402-payment sign --paymentId <id> --selectedIndex <n> --json` -> returns the payment header.
   **This step spends money.**
5. Replay the same `POST` with that header. The response has `jobId` and `jobToken`. **Save both now**; they can't be recovered.
6. Poll `GET /x402/jobs/{jobId}` with header `X-Job-Token: <jobToken>` until the report is ready.

If we automate it later: put the call behind a `STOCK_AGENT_X402_ENABLED=false` default, a per-day call cap
well under the $20 quota, and show "paid second opinion (0.1 U)" in the explanation so the spend is visible.

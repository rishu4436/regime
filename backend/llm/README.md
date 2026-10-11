# `backend/llm` — Bot 5 — LLM & Explanation Layer

**Owner:** Bot 5 — LLM & Explanation Layer. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Make the agent understandable via Grok.

- Natural language → structured strategy / agent config
- Explain every buy / stay-out decision in plain language
- Summarize current regime and active strategy
- Answer user questions about status and performance
- Never directly execute trades

## Interface it must implement
Implement `backend.contracts.Explainer` in `explainer.py` (`DefaultExplainer`):

```python
explain_decision(decision: Decision) -> str
summarize_state(status: StatusSnapshot) -> str
parse_strategy_from_text(text, default_symbol=None) -> ManualStrategy | AutoConfig   # validated pydantic output
answer(question, status: StatusSnapshot) -> str
```
Uses the xAI Grok API when `XAI_API_KEY` is set; otherwise (and on any API error/timeout) falls back to a
**deterministic template** implementation so tests and demos work offline. NL parsing must return a model that
validates against the contracts (use the JSON Schema in `contracts/schema/`). This module has no access to the
exchange client and must never place orders.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

## Implementation (Bot 5)

| File | What |
|---|---|
| `explainer.py` | `DefaultExplainer(client=None, *, use_llm=None)` — the `Explainer`. Tries Grok, falls back to templates on any error. `last_source` = `"grok"` / `"template"`, `last_error` for logs. |
| `client.py` | `GrokClient` / `GrokConfig` — OpenAI-compatible `POST {XAI_BASE_URL}/chat/completions` via httpx; all failures → `LLMError`. Key never logged/repr'd. |
| `prompts.py` | Prompt builders; facts are passed only as contract-model JSON; system prompt forbids claiming/placing trades. |
| `templates.py` | Deterministic offline text for explanations, summaries and Q&A (Q&A refuses trade requests). |
| `parser.py` | `keyword_parse` (deterministic NL → `ManualStrategy`/`AutoConfig`) and `validate_llm_strategy` (extract JSON → prune unknown keys → drop invalid fields → validate against the contract). |

**Env:** `XAI_API_KEY` (enables Grok; unset → templates), `XAI_MODEL` (default `grok-4.7`, per docs.x.ai model guide),
`XAI_BASE_URL` (default `https://api.x.ai/v1`), `XAI_TIMEOUT_S` (default `20`), `LLM_DISABLED=1` (force templates).

**Safety:** no imports of exchange/risk/app/engine code (enforced by a static test); parsed strategies always use
`activation=CONFIRMATION` unless the user's own text explicitly asks for auto-execution; LLM-supplied `id`/`created_at`/`version` are ignored.

**NL parsing:** Grok replies `{"kind": "manual"|"auto", "strategy": {...}}`; invalid/unrepairable output falls back to `keyword_parse`.
Listing entry conditions (e.g. "RSI below 30 and above the 200 SMA") enables only those; unspecified text → simulator defaults.

### Contracts v0.2.0
- `explain_decision` uses `Signal.exit_reason`, `Signal.position_id` and the fill price (`execution.avg_price` when filled,
  else `Signal.price` = intended fill), e.g. "Sold position ab12cd34 of NVDAon at 153.6 because the trailing stop was hit."
  Gap fills note the intended level; failed/expired/blocked exits are never described as sold.
- `StatusSnapshot` does not carry `PerformanceSummary` or `Alert`s, so `summarize_state` and `answer` accept optional
  keyword-only extras (protocol-compatible): `summarize_state(status, *, performance=None, alerts=None)`,
  `answer(question, status, *, performance=None, alerts=None)`. Templates show return / drawdown / win rate / loss streak
  and the 3 most recent alerts; Grok prompts get the same data as JSON (equity curve trimmed to 20 points, last 10 alerts).

## LIVE mode (real funds, autonomous 24/7) - `backend/llm/live.py`

When `Decision.mode == LIVE` (or the execution has `mode=LIVE` / `real_funds=True`) explanations switch to live wording:
the agent acts on its own, with **no confirmation/approval wording** (Grok output containing it is rejected and the
template is used instead).

- Fills: "The agent bought NVDAB on its own with real funds on BNB Chain mainnet: 24.99 USDT via SWAP at a fill price of
  182.4 (...). Price impact 0.31%. Tx hash 0x...: https://bscscan.com/tx/0x...". Fields are read defensively from the
  execution (attribute, pydantic extra or dict): `tx_hash|txHash`, `explorer_url|explorerUrl` (must start with
  `https://bscscan.com/tx/`, else rebuilt from a real 66-char tx hash), `route|path`, `real_funds|realFunds`,
  `amount_usdt|notional_usdt|amount|notional`, `fill_price|avg_price`, `price_impact_pct|priceImpactPercent` (also on a
  nested `quote`). If Grok omits the tx link, it is appended.
- Risk blocks: Bot 3 `"Blocked (<check>): <detail>"` reasons become plain language naming the limit, keeping the
  numbers, and saying whether the agent's risk limits or the Agentic Wallet set a cap. Size cuts on approved trades are
  explained too ("size cut from 30.00 to 25.00 USDT by the per-order cap (set by the agent's risk limits)").
- Engine STAY_OUT codes (Bot 2 `live_*`, `insufficient_data`) and Binance Web3 error codes in `ExecutionResult.error`
  (40367/40369 market closed, 40374 liquidity, 40375 Ondo 20 USD minimum, 40401 quote expired, ...) are mapped.
  Unknown reasons pass through cleaned; reserve/quota/allowance wording maps to the exit-quota reserve.
- Execution status `PENDING_CONFIRMATION` = submitted on-chain, waiting to settle (no approval wording). Price impact
  is a percent (`0.35` = 0.35%); `None` is shown as "Price impact unknown". Bot 3 warnings `exit_quota_shortfall`,
  `wallet_limits_stale` and wallet session expiry ("paused until Rishu re-scans the QR code") are mapped.
- `live.handled_codes()` lists every mapped code.

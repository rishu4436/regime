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

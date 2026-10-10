# `backend/mocks` — coordinator only

- `generate_candles(regime, n=250, seed=42, start_price=100.0, timeframe=Timeframe.H1)` — deterministic candles shaped like
  each of the six regimes (same args → identical output).
- `MockExchangeClient(regimes=None, seed=42, mode=SIM, clock=utc_now)` — offline `ExchangeClient`: tokens NVDAon/TSLAx/AAPLB/SPYon/METAx
  (default regimes: strong uptrend / high vol / sideways / mild uptrend / oversold), 30s quote TTL (EXPIRED on stale quotes),
  0.1% swap / 0.05% RFQ fee, refuses LIVE. `set_regime()` / `set_price()` for test scenarios; `executions` keeps a fill log.

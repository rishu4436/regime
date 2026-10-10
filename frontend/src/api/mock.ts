// In-browser mock of Bot 6's API: seeded candles, a live bar loop every few seconds, decisions with
// reasons, confirmation flow, pause/resume/emergency stop, overrides, and client-side backtests.
import {
  DEFAULT_INITIAL_CAPITAL,
  type AutoConfig, type BacktestResult, type Candle, type ControlState, type Decision, type Event, type ExecutionResult,
  type ManualStrategy, type OrderRequest, type Portfolio, type PresetName, type Position, type Regime, type StatusSnapshot, type Timeframe, type TokenInfo,
} from "../contracts";
import { detectRegime, presetWantsBuy, STYLE_MAP } from "../lib/auto";
import { appendCandle, generateCandles, rng } from "../lib/candles";
import { backtest, evaluateLast, fillStrategy, type FullStrategy } from "../lib/strategy";
import type { ApiClient, BacktestRequest } from "./types";

const TOKENS: TokenInfo[] = [
  { symbol: "NVDAon", name: "NVIDIA (Ondo tokenized)", token_type: "ONDO", chain: "Ethereum", underlying: "NVDA", decimals: 18 },
  { symbol: "TSLAx", name: "Tesla xStock", token_type: "XSTOCK", chain: "Solana", underlying: "TSLA", decimals: 8 },
  { symbol: "AAPLB", name: "Apple BStock", token_type: "BSTOCK", chain: "BSC", underlying: "AAPL", decimals: 18 },
];
const START: Record<string, number> = { NVDAon: 140, TSLAx: 200, AAPLB: 170 };
const SHAPE: Record<string, Regime> = { NVDAon: "MILD_UPTREND", TSLAx: "HIGH_VOLATILITY", AAPLB: "SIDEWAYS" };
const FEE = 0.001;
const uid = () => Math.random().toString(16).slice(2) + Date.now().toString(16);
const now = () => new Date().toISOString();
const delay = <T,>(v: T, ms = 150) => new Promise<T>((r) => setTimeout(() => r(structuredClone(v)), ms));

export class MockApi implements ApiClient {
  readonly kind = "mock" as const;
  private candles: Record<string, Candle[]> = {};
  private rand = rng("live");
  private listeners = new Set<(e: Event) => void>();
  private timer: number | undefined;
  private control: ControlState = { paused: false, emergency_stopped: false, confirmation_mode: true, trading_mode: "SIM", reason: null, updated_at: now() };
  private portfolio: Portfolio = { starting_capital: DEFAULT_INITIAL_CAPITAL, cash: DEFAULT_INITIAL_CAPITAL, positions: [], realized_pnl: 0, equity: DEFAULT_INITIAL_CAPITAL, bars_since_exit: {}, trades_today: 0, daily_pnl: 0, mode: "SIM", updated_at: now() };
  private agentMode: "MANUAL" | "AUTO" | null = null;
  private manual: FullStrategy | null = null;
  private auto: AutoConfig | null = null;
  private symbol = "NVDAon";
  private regime: Regime | null = null;
  private strategyLabel: string | null = null;
  private decisions: Decision[] = [];
  private barsSinceEntry: Record<string, number> = {};
  private trails: Record<string, number> = {};

  constructor(private tickMs = 3000) {
    // live series ends at the current hour; each mock tick then advances simulated time by one 1h bar
    const startTime = Math.floor(Date.now() / 3_600_000) * 3_600_000 - 249 * 3_600_000;
    for (const t of TOKENS) this.candles[t.symbol] = generateCandles({ regime: SHAPE[t.symbol], startPrice: START[t.symbol], n: 250, seed: 7, startTime });
    this.regime = detectRegime(this.candles[this.symbol]).regime;
    this.markToMarket();
  }

  // ------------------------------------------------------------------ reads
  health() { return delay({ status: "ok", trading_mode: "SIM" }); }
  getStatus() { return delay(this.snapshot()); }
  getTokens(q = "") {
    const ql = q.toLowerCase();
    return delay(TOKENS.filter((t) => !ql || t.symbol.toLowerCase().includes(ql) || t.name.toLowerCase().includes(ql))
      .map((t) => ({ ...t, price: this.last(t.symbol), volume_24h: this.candles[t.symbol].slice(-24).reduce((a, c) => a + c.volume * c.close, 0) })));
  }
  getCandles(symbol: string, _tf: Timeframe = "1h", limit = 250) { return delay((this.candles[symbol] ?? []).slice(-limit)); }

  // ------------------------------------------------------------------ strategy
  activateManual(s: ManualStrategy) {
    this.manual = fillStrategy(s); this.auto = null; this.agentMode = "MANUAL"; this.symbol = this.manual.symbol;
    this.strategyLabel = `MANUAL:${this.manual.name}`;
    this.setControl({ confirmation_mode: this.manual.activation === "CONFIRMATION" });
    this.emit("STATUS", { message: `Manual strategy "${this.manual.name}" activated on ${this.symbol}` });
    this.ensureLoop();
    return delay(this.snapshot());
  }
  activateAuto(c: AutoConfig) {
    this.auto = { ...c }; this.manual = null; this.agentMode = "AUTO"; this.symbol = c.symbol;
    this.setControl({ confirmation_mode: (c.activation ?? "CONFIRMATION") === "CONFIRMATION" });
    this.emit("STATUS", { message: `Auto mode (${c.style ?? "BALANCED"}) activated on ${c.symbol}` });
    this.ensureLoop();
    this.tick(false);
    return delay(this.snapshot());
  }
  backtest(req: BacktestRequest): Promise<BacktestResult> {
    const s = fillStrategy(req.strategy);
    const candles = generateCandles({ regime: SHAPE[s.symbol] ?? "SIDEWAYS", startPrice: START[s.symbol] ?? 100, n: req.limit ?? 250, seed: 42, timeframe: s.timeframe });
    const { buyBars: _b, sellBars: _s, ...res } = backtest(s, candles, req.initial_capital);
    return delay(res, 300);
  }
  /** Candles used by the mock backtest (so the chart matches the trades). */
  backtestCandles(symbol: string, timeframe: Timeframe = "1h", n = 250): Candle[] {
    return generateCandles({ regime: SHAPE[symbol] ?? "SIDEWAYS", startPrice: START[symbol] ?? 100, n, seed: 42, timeframe });
  }

  // ------------------------------------------------------------------ controls
  pause(reason = "") { return delay(this.setControl({ paused: true, reason: reason || "Paused by user" })); }
  resume() {
    if (this.control.emergency_stopped) return delay(this.setControl({ emergency_stopped: false, paused: false, reason: "Resumed after emergency stop" }));
    return delay(this.setControl({ paused: false, reason: null }));
  }
  emergencyStop(reason = "") {
    for (const p of [...(this.portfolio.positions ?? [])]) this.fill(p.symbol, "SELL", p.quantity * this.last(p.symbol), p.quantity, "EMERGENCY_STOP");
    for (const d of this.decisions) if (d.execution?.status === "PENDING_CONFIRMATION") d.execution = { ...d.execution, status: "REJECTED", error: "emergency stop" };
    return delay(this.setControl({ emergency_stopped: true, paused: true, reason: reason || "Emergency stop: all positions closed" }));
  }
  setConfirmationMode(enabled: boolean) { return delay(this.setControl({ confirmation_mode: enabled })); }

  confirmDecision(id: string) {
    const d = this.decisions.find((x) => x.id === id);
    if (!d || d.execution?.status !== "PENDING_CONFIRMATION") return Promise.reject(new Error("No pending trade with that id"));
    if (this.control.emergency_stopped) return Promise.reject(new Error("Emergency stop active"));
    d.execution = this.fill(d.symbol, d.action === "SELL" ? "SELL" : "BUY", d.signal?.proposed_notional ?? 0, null, null, d.strategy);
    d.reason += " — confirmed by user";
    this.emit("DECISION", d as unknown as Record<string, unknown>);
    return delay(d);
  }
  rejectDecision(id: string) {
    const d = this.decisions.find((x) => x.id === id);
    if (!d || !d.execution) return Promise.reject(new Error("No pending trade with that id"));
    d.execution = { ...d.execution, status: "REJECTED", error: "rejected by user" };
    this.emit("DECISION", d as unknown as Record<string, unknown>);
    return delay(d);
  }
  override(o: OrderRequest) {
    const r = this.fill(o.symbol, o.side, o.notional, o.quantity ?? null, o.side === "SELL" ? "MANUAL" : null, "MANUAL_OVERRIDE");
    this.log({ action: o.side, symbol: o.symbol, strategy: "MANUAL_OVERRIDE", reason: `Manual override by user: ${o.side} ${o.symbol}`, execution: r });
    return delay(r);
  }

  subscribe(fn: (e: Event) => void) {
    this.listeners.add(fn);
    this.ensureLoop();
    return () => { this.listeners.delete(fn); if (!this.listeners.size) { window.clearInterval(this.timer); this.timer = undefined; } };
  }

  // ------------------------------------------------------------------ internals
  private ensureLoop() { if (this.timer === undefined) this.timer = window.setInterval(() => this.tick(true), this.tickMs); }
  /** Simulated clock = open time of the latest bar (decisions line up with chart bars). */
  private simNow() { const c = this.candles[this.symbol]; return c[c.length - 1].timestamp; }
  private last(sym: string) { const c = this.candles[sym]; return c[c.length - 1].close; }
  private emit(type: Event["type"], payload: Record<string, unknown>) {
    const e: Event = { type, payload, timestamp: now() };
    this.listeners.forEach((fn) => fn(e));
  }
  private setControl(p: Partial<ControlState>) {
    this.control = { ...this.control, ...p, updated_at: now() };
    this.emit("CONTROL", this.control as Record<string, unknown>);
    return this.control;
  }
  private snapshot(): StatusSnapshot {
    return { control: this.control, portfolio: this.portfolio, agent_mode: this.agentMode, active_symbol: this.symbol,
      current_regime: this.regime, active_strategy: this.strategyLabel, last_decision: this.decisions[0] ?? null,
      recent_decisions: this.decisions.slice(0, 50), timestamp: now() };
  }
  private markToMarket() {
    const ps = this.portfolio.positions ?? [];
    let mv = 0;
    for (const p of ps) { p.current_price = this.last(p.symbol); p.highest_price = Math.max(p.highest_price ?? 0, p.current_price); p.unrealized_pnl = (p.current_price - p.avg_entry_price) * p.quantity; mv += p.current_price * p.quantity; }
    this.portfolio.equity = this.portfolio.cash + mv;
    this.portfolio.updated_at = now();
  }

  private fill(symbol: string, side: "BUY" | "SELL", notional: number, qty: number | null, exitReason: string | null, strategy = "MANUAL"): ExecutionResult {
    const px = this.last(symbol), pf = this.portfolio, ps = pf.positions ?? (pf.positions = []);
    const base: ExecutionResult = { order_id: uid(), client_order_id: uid(), symbol, side, status: "FILLED", path: "SWAP", mode: "SIM", timestamp: now() };
    if (side === "BUY") {
      notional = Math.min(notional, pf.cash);
      if (notional <= 1) return { ...base, status: "REJECTED", error: "insufficient cash" };
      const q = (notional * (1 - FEE)) / px;
      const ex = ps.find((p) => p.symbol === symbol);
      if (ex) { ex.avg_entry_price = (ex.avg_entry_price * ex.quantity + px * q) / (ex.quantity + q); ex.quantity += q; }
      else {
        const pos: Position = { symbol, quantity: q, avg_entry_price: px, opened_at: now(), strategy, current_price: px, highest_price: px };
        if (this.manual) {
          pos.stop_loss_price = px * (1 - this.manual.exit.stop_loss_pct / 100);
          pos.take_profit_price = px * (1 + this.manual.exit.take_profit_pct / 100);
          if (this.manual.exit.trailing_stop.enabled) { pos.trailing_stop_pct = this.manual.exit.trailing_stop.pct; this.trails[symbol] = px * (1 - pos.trailing_stop_pct / 100); }
        } else { pos.stop_loss_price = px * 0.94; pos.take_profit_price = px * 1.12; }
        ps.push(pos);
      }
      pf.cash -= notional; pf.trades_today = (pf.trades_today ?? 0) + 1; this.barsSinceEntry[symbol] = 0;
      this.markToMarket();
      return { ...base, filled_quantity: q, avg_price: px, notional, fee: notional * FEE };
    }
    const pos = ps.find((p) => p.symbol === symbol);
    if (!pos) return { ...base, status: "REJECTED", error: `no open ${symbol} position` };
    const q = Math.min(qty ?? notional / px, pos.quantity), gross = q * px, fee = gross * FEE;
    const pnl = (px - pos.avg_entry_price) * q - fee;
    pf.cash += gross - fee; pf.realized_pnl = (pf.realized_pnl ?? 0) + pnl; pf.daily_pnl = (pf.daily_pnl ?? 0) + pnl;
    pos.quantity -= q;
    if (pos.quantity <= 1e-9) { pf.positions = ps.filter((p) => p !== pos); pf.bars_since_exit = { ...pf.bars_since_exit, [symbol]: 0 }; delete this.trails[symbol]; }
    pf.trades_today = (pf.trades_today ?? 0) + 1;
    this.markToMarket();
    this.emit("EXECUTION", { symbol, side, pnl, exit_reason: exitReason });
    return { ...base, filled_quantity: q, avg_price: px, notional: gross, fee };
  }

  private log(d: Omit<Decision, "mode" | "agent_mode"> & { agent_mode?: Decision["agent_mode"] }): Decision {
    const full: Decision = { id: uid(), timestamp: this.simNow(), mode: "SIM", agent_mode: d.agent_mode ?? this.agentMode ?? "MANUAL", regime: this.regime, ...d };
    full.explanation ??= full.reason;
    this.decisions.unshift(full);
    this.decisions = this.decisions.slice(0, 100);
    this.emit("DECISION", full as unknown as Record<string, unknown>);
    return full;
  }

  private tick(advance: boolean) {
    if (advance) for (const t of TOKENS) {
      const c = this.candles[t.symbol];
      c.push(appendCandle(c, this.rand, SHAPE[t.symbol]));
      if (c.length > 500) c.shift();
      this.barsSinceEntry[t.symbol] = (this.barsSinceEntry[t.symbol] ?? 99) + 1;
      const bse = this.portfolio.bars_since_exit ?? {};
      if (bse[t.symbol] != null) bse[t.symbol] += 1;
    }
    const c = this.candles[this.symbol], px = this.last(this.symbol);
    const reg = detectRegime(c);
    this.regime = reg.regime;
    this.markToMarket();
    this.emit("PRICE", { symbol: this.symbol, price: px });

    if (this.control.emergency_stopped || !this.agentMode) { this.emit("STATUS", {}); return; }

    // 1. exits (risk layer)
    for (const p of [...(this.portfolio.positions ?? [])]) {
      const cp = this.last(p.symbol);
      let why: string | null = null;
      if (p.trailing_stop_pct && this.trails[p.symbol] != null) {
        if (cp < this.trails[p.symbol]) why = "TRAILING_STOP";
        else this.trails[p.symbol] = Math.max(this.trails[p.symbol], cp * (1 - p.trailing_stop_pct / 100));
      }
      if (!why && p.stop_loss_price && cp <= p.stop_loss_price) why = "STOP_LOSS";
      if (!why && p.take_profit_price && cp >= p.take_profit_price) why = "TAKE_PROFIT";
      if (why) {
        const r = this.fill(p.symbol, "SELL", 0, p.quantity, why, p.strategy);
        this.log({ symbol: p.symbol, action: "SELL", strategy: p.strategy, reason: `${why.replace("_", " ").toLowerCase()} hit at ${cp.toFixed(2)}`, execution: r });
      }
    }
    if (this.control.paused) { this.log({ symbol: this.symbol, action: "STAY_OUT", strategy: this.strategyLabel ?? "-", reason: `Paused: ${this.control.reason ?? "by user"}` }); return; }
    if (this.decisions.some((d) => d.execution?.status === "PENDING_CONFIRMATION")) return; // wait for the user

    // 2. strategy
    let buy = false, reason = "", sizePct = 15, strategy = this.strategyLabel ?? "-", preset: PresetName | null = null;
    const met: string[] = [], failed: string[] = [];
    const open = (this.portfolio.positions ?? []).filter((p) => p.symbol === this.symbol).length;
    if (this.agentMode === "MANUAL" && this.manual) {
      const m = this.manual, cool = (this.portfolio.bars_since_exit ?? {})[this.symbol];
      sizePct = m.risk.position_size_pct;
      if (open >= m.risk.max_open_positions) reason = `Max open positions (${m.risk.max_open_positions}) reached`;
      else if (cool != null && cool <= m.risk.cooldown_bars) reason = `Cooldown: ${cool}/${m.risk.cooldown_bars} bars since last exit`;
      else {
        const ev = evaluateLast(m, c);
        met.push(...ev.met); failed.push(...ev.failed);
        buy = ev.ok;
        reason = ev.blocked ? `Entry blocked: ${ev.blocked}` : buy ? `Entry (${m.entry.logic}) met: ${ev.met.join(", ")}` : `Entry (${m.entry.logic}) not met: ${ev.failed.join(", ") || "-"}`;
      }
    } else if (this.agentMode === "AUTO" && this.auto) {
      const style = this.auto.style ?? "BALANCED";
      const [p, mult] = STYLE_MAP[reg.regime][style];
      const enabled = this.auto.enabled_presets ?? [];
      if (!p || (enabled.length && !enabled.includes(p))) {
        strategy = "NONE"; this.strategyLabel = "STAY OUT";
        reason = `${reg.regime} + ${style} → no preset${p ? ` (${p} disabled)` : ""}; staying out. ${reg.reason}`;
      } else {
        preset = p; strategy = p; this.strategyLabel = p; sizePct = 10 * mult;
        if (open >= 3) reason = `${p}: max exposure (3 entries) reached`;
        else { const w = presetWantsBuy(p, c, this.barsSinceEntry[this.symbol] ?? 99); buy = w.buy; reason = `${reg.regime} + ${style} → ${p}: ${w.reason}`; }
      }
    }
    if (!buy) { this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason }); return; }

    // 3. risk + execution
    const notional = this.portfolio.equity * (sizePct / 100);
    const signal = { id: uid(), symbol: this.symbol, action: "BUY" as const, source: this.agentMode, strategy, preset, regime: reg.regime, reason, conditions_met: met, conditions_failed: failed, price: px, proposed_size_pct: sizePct, proposed_notional: notional, timestamp: now() };
    if (notional > this.portfolio.cash) {
      this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason: `Risk blocked: insufficient cash for ${notional.toFixed(0)} USDT`, signal,
        risk_verdict: { signal_id: signal.id, approved: false, reasons: ["insufficient cash"] } });
      return;
    }
    const verdict = { signal_id: signal.id, approved: true, reasons: [], adjusted_size_pct: sizePct, adjusted_notional: notional, requires_confirmation: !!this.control.confirmation_mode };
    if (this.control.confirmation_mode) {
      this.log({ symbol: this.symbol, action: "BUY", strategy, reason: `${reason} — awaiting your confirmation`, signal, risk_verdict: verdict,
        execution: { client_order_id: uid(), symbol: this.symbol, side: "BUY", status: "PENDING_CONFIRMATION", path: "SWAP", mode: "SIM", notional, avg_price: px } });
      return;
    }
    const r = this.fill(this.symbol, "BUY", notional, null, null, strategy);
    this.log({ symbol: this.symbol, action: "BUY", strategy, reason, signal, risk_verdict: verdict, execution: r });
  }
}

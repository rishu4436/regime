// In-browser mock of Bot 6's API (backend/app/API.md): same methods, payloads, error codes (404/409/422) and
// WebSocket-style Events (STATUS, DECISION, EXECUTION, CONTROL, PRICE, ALERT) emitted on a timer by a SIM loop.
import {
  CONTRACTS_VERSION, DEFAULT_INITIAL_CAPITAL, PresetNameValues, RegimeValues, TraderStyleValues,
  type Alert, type AlertLevel, type BacktestRequest, type LoopState, type PerformanceSummary, type SystemInfo, type AutoConfig, type BacktestResult, type Candle, type ControlState, type Decision, type EquityPoint, type Event, type ExecutionResult,
  type ExitReason, type ManualStrategy, type Portfolio, type PresetName, type Position, type Regime, type RegimeAnalysis, type Signal,
  type StatusSnapshot, type StrategySelection, type Timeframe, type TokenInfo, type TraderStyle,
} from "../contracts";
import { detectRegime, presetWantsBuy, STYLE_MAP } from "../lib/auto";
import { appendCandle, generateMixedCandles, rng } from "../lib/candles";
import { parseStrategyText } from "../lib/parseText";
import { backtest, conditionLabel, evaluateLast, fillStrategy, type FullStrategy } from "../lib/strategy";
import { ApiError, type ActivateManualBody, type ApiClient, type ParseResult, type PresetsTable } from "./types";

const TOKENS: TokenInfo[] = [
  { symbol: "NVDAon", name: "NVIDIA (Ondo tokenized)", token_type: "ONDO", chain: "Ethereum", underlying: "NVDA", decimals: 18 },
  { symbol: "TSLAx", name: "Tesla xStock", token_type: "XSTOCK", chain: "Solana", underlying: "TSLA", decimals: 8 },
  { symbol: "AAPLB", name: "Apple BStock", token_type: "BSTOCK", chain: "BSC", underlying: "AAPL", decimals: 18 },
];
const START: Record<string, number> = { NVDAon: 140, TSLAx: 200, AAPLB: 170 };
const LIVE_REGIMES: Regime[] = ["MILD_UPTREND", "SIDEWAYS", "OVERSOLD_PULLBACK", "STRONG_UPTREND", "HIGH_VOLATILITY", "DOWNTREND"];
const FEE = 0.001;
const uid = () => Math.random().toString(16).slice(2, 10) + Date.now().toString(16);
const now = () => new Date().toISOString();
const ok = <T,>(v: T, ms = 120) => new Promise<T>((r) => setTimeout(() => r(structuredClone(v)), ms));
const fail = (status: number, detail: string) => new Promise<never>((_, rej) => setTimeout(() => rej(new ApiError(status, detail)), 80));

export class MockApi implements ApiClient {
  readonly kind = "mock" as const;
  private candles: Record<string, Candle[]> = {};
  private rand = rng("live");
  private listeners = new Set<(e: Event) => void>();
  private timer: number | undefined;
  private loop: LoopState = { running: false, interval_seconds: 3, ticks: 0, agent_mode: null, active_symbol: null, active_strategy: null, replay_finished: false };
  private control: ControlState = { paused: false, emergency_stopped: false, confirmation_mode: true, trading_mode: "SIM", reason: null, updated_at: now() };
  private portfolio: Portfolio = { starting_capital: DEFAULT_INITIAL_CAPITAL, cash: DEFAULT_INITIAL_CAPITAL, positions: [], realized_pnl: 0, equity: DEFAULT_INITIAL_CAPITAL, bars_since_exit: {}, trades_today: 0, daily_pnl: 0, mode: "SIM", updated_at: now() };
  private agentMode: "MANUAL" | "AUTO" | null = null;
  private manual: FullStrategy | null = null;
  private auto: AutoConfig | null = null;
  private symbol = "NVDAon";
  private regime: RegimeAnalysis | null = null;
  private activeStrategy: string | null = null;
  private decisions: Decision[] = [];
  private alerts: Alert[] = [];
  private strategies = new Map<string, ManualStrategy>();
  private equityCurve: EquityPoint[] = [];
  private closed: number[] = []; // realised PnL per closed trade
  private barsSinceEntry: Record<string, number> = {};
  private trails: Record<string, number> = {};

  constructor() {
    // live series ends at the current hour; each loop tick advances simulated time by one 1h bar
    const startTime = Math.floor(Date.now() / 3_600_000) * 3_600_000 - 249 * 3_600_000;
    for (const t of TOKENS) this.candles[t.symbol] = generateMixedCandles(t.symbol, START[t.symbol], 250, "1h", 7, startTime);
    this.regime = this.analyse(this.symbol);
    this.markToMarket();
    try { for (const s of JSON.parse(localStorage.getItem("mock.strategies") ?? "[]")) this.strategies.set(s.id, s); } catch { /* ignore */ }
  }

  // ------------------------------------------------------------------ system & market data
  health() { return ok({ status: "ok", contracts_version: CONTRACTS_VERSION, trading_mode: "SIM" }); }
  system() {
    return ok<SystemInfo>({ trading_mode: "SIM", live_enabled: false, contracts_version: CONTRACTS_VERSION, sim_seed: 42,
      components: { engine: "mock:browser", risk: "mock:browser", exchange: "mock:browser", explainer: "mock:template" } });
  }
  getTokens(q = "") {
    const ql = q.toLowerCase();
    return ok(TOKENS.filter((t) => !ql || t.symbol.toLowerCase().includes(ql) || t.name.toLowerCase().includes(ql))
      .map((t) => ({ ...t, price: this.last(t.symbol), volume_24h: this.candles[t.symbol].slice(-24).reduce((a, c) => a + c.volume * c.close, 0) })));
  }
  getCandles(symbol: string, _tf: Timeframe = "1h", limit = 200) {
    return this.candles[symbol] ? ok(this.candles[symbol].slice(-limit)) : fail(404, `unknown symbol ${symbol}`);
  }
  getPrice(symbol: string) { return this.candles[symbol] ? ok({ symbol, price: this.last(symbol) }) : fail(404, `unknown symbol ${symbol}`); }
  getRegime(symbol: string) { return this.candles[symbol] ? ok(this.analyse(symbol)) : fail(404, `unknown symbol ${symbol}`); }
  getSelection(symbol: string, style: TraderStyle = "BALANCED") {
    if (!this.candles[symbol]) return fail(404, `unknown symbol ${symbol}`);
    return ok(this.select(this.analyse(symbol), style, [...PresetNameValues]));
  }

  getPresetsTable() {
    const entries = RegimeValues.flatMap((regime) => TraderStyleValues.map((style) => {
      const sel = this.select({ regime, confidence: 1, reason: "" }, style, [...PresetNameValues]);
      return { regime, style, preset: sel.preset ?? null, size_multiplier: sel.size_multiplier ?? 0, reason: sel.reason };
    }));
    return ok<PresetsTable>({ source: "fallback:MockApi", entries });
  }

  // ------------------------------------------------------------------ strategies
  listStrategies() { return ok([...this.strategies.values()]); }
  saveStrategy(s: ManualStrategy) {
    const v = this.validate(s); if (typeof v === "string") return fail(422, v);
    const saved = { ...v, id: v.id ?? uid(), created_at: v.created_at ?? now() };
    this.strategies.set(saved.id!, saved); this.persist();
    return ok(saved);
  }
  getStrategy(id: string) { const s = this.strategies.get(id); return s ? ok(s) : fail(404, `strategy ${id} not found`); }
  updateStrategy(id: string, s: ManualStrategy) {
    const cur = this.strategies.get(id); if (!cur) return fail(404, `strategy ${id} not found`);
    const v = this.validate(s); if (typeof v === "string") return fail(422, v);
    const saved = { ...v, id, created_at: cur.created_at, version: (cur.version ?? 1) + 1 };
    this.strategies.set(id, saved); this.persist();
    return ok(saved);
  }
  deleteStrategy(id: string) {
    if (!this.strategies.delete(id)) return fail(404, `strategy ${id} not found`);
    this.persist(); return ok({ deleted: id });
  }
  validateStrategy(json: unknown) { const v = this.validate(json); return typeof v === "string" ? fail(422, v) : ok(v); }
  backtest(req: BacktestRequest): Promise<BacktestResult> {
    const v = this.validate(req.strategy); if (typeof v === "string") return fail(422, v);
    if (req.initial_capital != null && !(req.initial_capital > 0)) return fail(422, "initial_capital: must be > 0");
    const s = fillStrategy(v);
    const candles = generateMixedCandles(s.symbol, START[s.symbol] ?? 100, req.limit ?? 500, s.timeframe);
    const { buyBars: _b, sellBars: _s, ...res } = backtest(s, candles, req.initial_capital ?? DEFAULT_INITIAL_CAPITAL);
    return ok(res, 300);
  }
  /** Same seeded series the mock backtest used, so the chart lines up with the trades. */
  backtestCandles(symbol: string, timeframe: Timeframe = "1h", limit = 500) {
    return ok(generateMixedCandles(symbol, START[symbol] ?? 100, limit, timeframe));
  }

  // ------------------------------------------------------------------ agent & loop
  activateManual(body: ActivateManualBody) {
    let src: unknown;
    if ("strategy_id" in body) { src = this.strategies.get(body.strategy_id); if (!src) return fail(404, `strategy ${body.strategy_id} not found`); }
    else src = body.strategy;
    const v = this.validate(src); if (typeof v === "string") return fail(422, v);
    this.manual = fillStrategy(v); this.auto = null; this.agentMode = "MANUAL"; this.symbol = this.manual.symbol;
    this.activeStrategy = `MANUAL:${this.manual.name}`;
    this.setControl({ confirmation_mode: this.manual.activation === "CONFIRMATION" });
    this.rejectAllPending("agent switched");
    this.syncLoop(); this.emitStatus();
    return ok(this.snapshot());
  }
  activateAuto(c: AutoConfig) {
    if (!this.candles[c.symbol]) return fail(404, `unknown symbol ${c.symbol}`);
    if (!(c.capital > 0)) return fail(422, "capital: must be greater than 0");
    if (c.enabled_presets && !c.enabled_presets.length) return fail(422, "enabled_presets: at least 1 item required");
    this.auto = { ...c }; this.manual = null; this.agentMode = "AUTO"; this.symbol = c.symbol;
    this.regime = this.analyse(c.symbol);
    this.activeStrategy = this.select(this.regime, c.style ?? "BALANCED", c.enabled_presets ?? [...PresetNameValues]).preset ?? "NONE";
    this.setControl({ confirmation_mode: (c.activation ?? "CONFIRMATION") === "CONFIRMATION" });
    this.rejectAllPending("agent switched");
    this.syncLoop(); this.emitStatus();
    return ok(this.snapshot());
  }
  deactivate() {
    this.agentMode = null; this.manual = null; this.auto = null; this.activeStrategy = null;
    this.rejectAllPending("agent deactivated");
    this.syncLoop(); this.emitStatus();
    return ok(this.snapshot());
  }
  getLoop() { return ok(this.loop); }
  startLoop(interval_seconds?: number) {
    if (interval_seconds != null && !(interval_seconds > 0)) return fail(422, "interval_seconds: must be > 0");
    if (!this.agentMode) return fail(409, "no active agent: activate a manual strategy or auto config first"); // same as backend
    this.loop = { ...this.loop, running: true, interval_seconds: interval_seconds ?? this.loop.interval_seconds };
    this.restartTimer(); this.emitStatus();
    return ok(this.loop);
  }
  stopLoop() { this.loop = { ...this.loop, running: false }; this.restartTimer(); this.emitStatus(); return ok(this.loop); }
  tick() {
    if (!this.agentMode) return fail(409, "no active agent");
    return ok(this.cycle());
  }

  // ------------------------------------------------------------------ decisions / confirmations / positions
  getStatus() { return ok(this.snapshot()); }
  getPortfolio() { this.markToMarket(); return ok(this.portfolio); }
  getDecisions(limit = 50, symbol?: string) { return ok(this.decisions.filter((d) => !symbol || d.symbol === symbol).slice(0, limit)); }
  getDecision(id: string) { const d = this.decisions.find((x) => x.id === id); return d ? ok(d) : fail(404, `decision ${id} not found`); }
  getConfirmations() { return ok(this.pending()); }
  approve(id: string) {
    const d = this.decisions.find((x) => x.id === id);
    // like the backend: anything no longer pending (approved, rejected, agent switched) is a 404
    if (!d || d.execution?.status !== "PENDING_CONFIRMATION") return fail(404, `no pending confirmation ${id}`);
    if (this.control.emergency_stopped) return fail(409, "emergency stop active");
    const r = this.buy(d.symbol, d.risk_verdict?.adjusted_notional ?? d.signal?.proposed_notional ?? 0, d.strategy, d.id);
    d.execution = r;

    if (r.status !== "FILLED") this.alert("WARNING", "EXECUTION_FAILED", `Approve failed: ${r.error}`, d.symbol);
    this.emit("DECISION", d); this.emitStatus();
    return ok(d);
  }
  reject(id: string, reason?: string, byUser = true) {
    const d = this.decisions.find((x) => x.id === id);
    if (!d || d.execution?.status !== "PENDING_CONFIRMATION") return fail(404, `no pending confirmation ${id}`);
    const who = byUser ? "Rejected by user" : "Not executed", text = reason ? `${who}: ${reason}` : who; // API.md v0.4 reason texts
    d.action = "STAY_OUT"; d.reason = text; d.explanation = text;
    d.execution = { ...d.execution!, status: "REJECTED", error: text };
    this.emit("DECISION", d); this.emitStatus();
    return ok(d);
  }
  /** A previous agent's confirmations never execute under a new agent: "Not executed: agent switched". */
  private rejectAllPending(why: string) { for (const d of this.pending()) this.reject(d.id!, why, false); }
  closePositionById(positionId: string) {
    const pos = (this.portfolio.positions ?? []).find((p) => p.id === positionId);
    if (!pos) return fail(404, `no open position with id ${positionId}`);
    const d = this.exitDecision(pos, "MANUAL", this.last(pos.symbol), `close requested by user (lot ${positionId.slice(0, 6)})`);
    this.emitStatus();
    return ok(d);
  }
  closePosition(symbol: string) {
    if (!this.candles[symbol]) return fail(404, `unknown symbol ${symbol}`);
    const lots = (this.portfolio.positions ?? []).filter((p) => p.symbol === symbol);
    if (!lots.length) return fail(409, `no open ${symbol} position`);
    let d: Decision | null = null;
    for (const pos of lots) d = this.exitDecision(pos, "MANUAL", this.last(symbol), `close requested by user (all ${symbol})`);
    this.emitStatus();
    return ok(d!);
  }

  // ------------------------------------------------------------------ control
  getControl() { return ok(this.control); }
  pause(reason?: string) {
    const c = this.setControl({ paused: true, reason: reason || "Paused by user" });
    this.alert("WARNING", "PAUSED", onePrefix("Paused", c.reason)); this.emitStatus(); return ok(c);
  }
  resume() { const c = this.setControl({ paused: false, reason: this.control.emergency_stopped ? this.control.reason : null }); this.emitStatus(); return ok(c); }
  resetEmergencyStop() {
    if (!this.control.emergency_stopped) return fail(409, "emergency stop is not active");
    const c = this.setControl({ emergency_stopped: false, reason: this.control.paused ? "Emergency stop reset; still paused" : null });
    this.alert("INFO", "EMERGENCY_STOP", "Emergency stop reset by user"); this.emitStatus(); return ok(c);
  }
  emergencyStop(reason?: string, flatten = false) {
    if (flatten) for (const p of [...(this.portfolio.positions ?? [])]) this.exitDecision(p, "EMERGENCY_STOP", this.last(p.symbol), "position flattened at the last price");
    for (const d of this.pending()) { d.action = "STAY_OUT"; d.execution = { ...d.execution!, status: "REJECTED", error: "emergency stop" }; }
    const c = this.setControl({ emergency_stopped: true, reason: reason || "Emergency stop" });
    this.alert("CRITICAL", "EMERGENCY_STOP", `${onePrefix("Emergency stop", c.reason)}${flatten ? " (all positions closed)" : ""}`);
    this.emitStatus();
    return ok(c);
  }
  setConfirmationMode(enabled: boolean) { const c = this.setControl({ confirmation_mode: enabled }); this.emitStatus(); return ok(c); }

  // ------------------------------------------------------------------ monitoring
  getAlerts(limit = 50) { return ok(this.alerts.slice(0, limit)); }
  getPerformance() {
    this.markToMarket();
    const pf = this.portfolio, unreal = (pf.positions ?? []).reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
    let peak = pf.starting_capital, mdd = 0;
    for (const e of this.equityCurve) { peak = Math.max(peak, e.equity); mdd = Math.max(mdd, (peak - e.equity) / peak); }
    const wins = this.closed.filter((x) => x > 0).length, losses = this.closed.length - wins;
    let consec = 0; for (let i = this.closed.length - 1; i >= 0 && this.closed[i] <= 0; i--) consec++;
    return ok<PerformanceSummary>({
      starting_capital: pf.starting_capital, equity: pf.equity, realized_pnl: pf.realized_pnl ?? 0, unrealized_pnl: unreal,
      total_return_pct: ((pf.equity - pf.starting_capital) / pf.starting_capital) * 100, max_drawdown_pct: mdd * 100,
      win_rate_pct: this.closed.length ? (wins / this.closed.length) * 100 : 0, num_trades: this.closed.length, wins, losses,
      consecutive_losses: consec, equity_curve: this.equityCurve.slice(-500),
    });
  }

  // ------------------------------------------------------------------ llm (mocked Bot 5)
  llmParse(text: string, default_symbol?: string) {
    if (!text.trim()) return fail(422, "text: must not be empty");
    const out = parseStrategyText(text, default_symbol);
    return ok<ParseResult>("entry" in out || "exit" in out ? { kind: "MANUAL", strategy: out as ManualStrategy } : { kind: "AUTO", config: out as AutoConfig }, 450);
  }
  llmAsk(question: string) {
    if (!question.trim()) return fail(422, "question: must not be empty");
    const q = question.toLowerCase(), last = this.decisions[0], pf = this.portfolio;
    let answer: string;
    if (/why|reason|last|decision/.test(q)) answer = last ? `Last decision on ${last.symbol}: ${last.action.replace("_", " ")}. ${last.reason}` : "No decisions yet; activate a strategy and the agent will explain each bar.";
    else if (/regime|market/.test(q)) answer = this.regime ? `${this.symbol} is in a ${this.regime.regime.replace(/_/g, " ").toLowerCase()} regime (${Math.round(this.regime.confidence * 100)}% confidence). ${this.regime.reason}` : "Regime unknown.";
    else if (/pnl|profit|loss|perform|equity/.test(q)) answer = `Equity ${pf.equity.toFixed(2)} USDT vs ${pf.starting_capital} start (${(((pf.equity - pf.starting_capital) / pf.starting_capital) * 100).toFixed(2)}%), realised ${(pf.realized_pnl ?? 0).toFixed(2)} USDT over ${this.closed.length} closed trades.`;
    else if (/position|holding/.test(q)) answer = pf.positions?.length ? pf.positions.map((p) => `${p.symbol}: ${p.quantity.toFixed(4)} @ ${p.avg_entry_price.toFixed(2)} (uPnL ${(p.unrealized_pnl ?? 0).toFixed(2)})`).join("; ") : "No open positions.";
    else if (/strategy|preset/.test(q)) answer = this.activeStrategy ? `Active: ${this.activeStrategy === "NONE" ? "staying out (no preset fits this regime and style)" : this.activeStrategy} on ${this.symbol}, ${this.agentMode} mode.` : "No strategy is active.";
    else answer = `I'm the SIM-mode agent (mock). Status: ${this.control.emergency_stopped ? "emergency stopped" : this.control.paused ? "paused" : "running"}, ${this.agentMode ?? "no"} agent on ${this.symbol}. Ask about the last decision, the regime, PnL, positions or the strategy.`;
    return ok({ answer }, 400);
  }
  llmSummary() {
    const pf = this.portfolio, r = this.regime;
    return ok({ summary: `${this.agentMode ? `${this.agentMode} agent running ${this.activeStrategy === "NONE" ? "but staying out" : this.activeStrategy} on ${this.symbol}` : "No agent active"}. ` +
      `Regime: ${r ? r.regime.replace(/_/g, " ").toLowerCase() : "unknown"}. Equity ${pf.equity.toFixed(0)} USDT (${(((pf.equity - pf.starting_capital) / pf.starting_capital) * 100).toFixed(2)}%), ` +
      `${pf.positions?.length ?? 0} open position(s), ${this.pending().length} trade(s) awaiting confirmation.` }, 300);
  }

  subscribe(fn: (e: Event) => void) {
    this.listeners.add(fn);
    fn({ type: "STATUS", payload: structuredClone(this.snapshot()) as unknown as Record<string, unknown>, timestamp: now() }); // like WS on-connect
    this.restartTimer();
    return () => { this.listeners.delete(fn); if (!this.listeners.size) this.restartTimer(); };
  }

  // ------------------------------------------------------------------ internals
  private persist() { try { localStorage.setItem("mock.strategies", JSON.stringify([...this.strategies.values()])); } catch { /* ignore */ } }
  private validate(json: unknown): ManualStrategy | string {
    if (!json || typeof json !== "object") return "body: expected a ManualStrategy object";
    const s = fillStrategy(json as ManualStrategy);
    if (!this.candles[s.symbol]) return `symbol: unknown token ${s.symbol}`;
    if (!(s.risk.position_size_pct > 0 && s.risk.position_size_pct <= 100)) return "risk.position_size_pct: must be in (0, 100]";
    if (s.risk.max_open_positions < 1) return "risk.max_open_positions: must be >= 1";
    if (s.exit.take_profit_pct <= 0 || s.exit.stop_loss_pct <= 0) return "exit: take_profit_pct and stop_loss_pct must be > 0";
    if (s.entry.rsi_below.threshold < 0 || s.entry.rsi_below.threshold > 100) return "entry.rsi_below.threshold: must be 0-100";
    return { ...s, id: (json as ManualStrategy).id, created_at: (json as ManualStrategy).created_at } as ManualStrategy;
  }
  private restartTimer() {
    window.clearInterval(this.timer); this.timer = undefined;
    if (this.loop.running && this.listeners.size) this.timer = window.setInterval(() => this.cycle(true), this.loop.interval_seconds * 1000);
  }
  private syncLoop() { this.loop = { ...this.loop, agent_mode: this.agentMode, active_symbol: this.agentMode ? this.symbol : null, active_strategy: this.activeStrategy }; }
  private last(sym: string) { const c = this.candles[sym]; return c[c.length - 1].close; }
  private simNow() { const c = this.candles[this.symbol]; return c[c.length - 1].timestamp; }
  private pending() { return this.decisions.filter((d) => d.execution?.status === "PENDING_CONFIRMATION"); }
  private emit(type: Event["type"], payload: unknown) {
    const e: Event = { type, payload: structuredClone(payload) as Record<string, unknown>, timestamp: now() };
    this.listeners.forEach((fn) => fn(e));
  }
  private emitStatus() { this.emit("STATUS", this.snapshot()); }
  private alert(level: AlertLevel, code: string, message: string, symbol?: string) {
    const a: Alert = { id: uid(), level, code, message, symbol: symbol ?? null, timestamp: now() };
    this.alerts.unshift(a); this.alerts = this.alerts.slice(0, 100);
    this.emit("ALERT", a);
  }
  private setControl(p: Partial<ControlState>) {
    this.control = { ...this.control, ...p, updated_at: now() };
    this.emit("CONTROL", this.control);
    return this.control;
  }
  private analyse(symbol: string): RegimeAnalysis {
    const r = detectRegime(this.candles[symbol]);
    return { regime: r.regime, confidence: r.confidence, reason: r.reason, features: {}, timestamp: now() };
  }
  private select(r: RegimeAnalysis, style: TraderStyle, enabled: PresetName[]): StrategySelection {
    const [p, m] = STYLE_MAP[r.regime][style];
    if (!p) return { regime: r.regime, style, preset: null, size_multiplier: 0, reason: `${r.regime} + ${style}: no preset fits, stay out` };
    if (!enabled.includes(p)) return { regime: r.regime, style, preset: null, size_multiplier: 0, reason: `${r.regime} + ${style} maps to ${p}, which is disabled: stay out` };
    return { regime: r.regime, style, preset: p, size_multiplier: m, reason: `${r.regime} + ${style} → ${p}${m !== 1 ? ` (size ×${m})` : ""}` };
  }
  private snapshot(): StatusSnapshot {
    return { control: this.control, portfolio: this.portfolio, agent_mode: this.agentMode, active_symbol: this.symbol,
      current_regime: this.regime?.regime ?? null, active_strategy: this.activeStrategy, last_decision: this.decisions[0] ?? null,
      recent_decisions: this.decisions.slice(0, 50), timestamp: now() };
  }
  private markToMarket() {
    let mv = 0;
    for (const p of this.portfolio.positions ?? []) {
      p.current_price = this.last(p.symbol); p.highest_price = Math.max(p.highest_price ?? 0, p.current_price);
      p.unrealized_pnl = (p.current_price - p.avg_entry_price) * p.quantity; mv += p.current_price * p.quantity;
    }
    this.portfolio.equity = this.portfolio.cash + mv;
    this.portfolio.updated_at = now();
  }

  private exec(symbol: string, side: "BUY" | "SELL"): ExecutionResult {
    return { order_id: uid(), client_order_id: uid(), symbol, side, status: "FILLED", path: "SWAP", mode: "SIM", timestamp: now() };
  }
  private done(r: ExecutionResult, decisionId?: string) { this.emit("EXECUTION", { ...r, decision_id: decisionId ?? null }); return r; }

  /** Open a new lot (several lots per symbol are allowed; each has a stable Position.id). */
  private buy(symbol: string, notional: number, strategy: string, decisionId?: string): ExecutionResult {
    const px = this.last(symbol), pf = this.portfolio, ps = pf.positions ?? (pf.positions = []), base = this.exec(symbol, "BUY");
    notional = Math.min(notional, pf.cash);
    if (notional <= 1) return this.done({ ...base, status: "REJECTED", error: "insufficient cash" }, decisionId);
    const q = (notional * (1 - FEE)) / px, x = this.manual?.exit;
    const pos: Position = { id: uid(), symbol, quantity: q, avg_entry_price: px, opened_at: now(), strategy, current_price: px, highest_price: px,
      stop_loss_price: px * (1 - (x?.stop_loss_pct ?? 6) / 100), take_profit_price: px * (1 + (x?.take_profit_pct ?? 12) / 100) };
    if (x?.trailing_stop.enabled) { pos.trailing_stop_pct = x.trailing_stop.pct; this.trails[pos.id!] = px * (1 - x.trailing_stop.pct / 100); }
    ps.push(pos);
    pf.cash -= notional; pf.trades_today = (pf.trades_today ?? 0) + 1; this.barsSinceEntry[symbol] = 0;
    this.markToMarket();
    return this.done({ ...base, filled_quantity: q, avg_price: px, notional, fee: notional * FEE }, decisionId);
  }

  /** Close one lot at `fillPx` (SL/TP level, gap open or market). */
  private sellLot(pos: Position, fillPx: number, decisionId?: string): ExecutionResult {
    const pf = this.portfolio, base = this.exec(pos.symbol, "SELL");
    const gross = pos.quantity * fillPx, fee = gross * FEE, pnl = (fillPx - pos.avg_entry_price) * pos.quantity - fee;
    pf.cash += gross - fee; pf.realized_pnl = (pf.realized_pnl ?? 0) + pnl; pf.daily_pnl = (pf.daily_pnl ?? 0) + pnl;
    pf.positions = (pf.positions ?? []).filter((p) => p !== pos);
    pf.bars_since_exit = { ...pf.bars_since_exit, [pos.symbol]: 0 }; delete this.trails[pos.id!];
    pf.trades_today = (pf.trades_today ?? 0) + 1;
    this.closed.push(pnl);
    let consec = 0; for (let i = this.closed.length - 1; i >= 0 && this.closed[i] <= 0; i--) consec++;
    if (consec >= 3) this.alert("WARNING", "CONSECUTIVE_LOSSES", `${consec} losing trades in a row`, pos.symbol);
    this.markToMarket();
    return this.done({ ...base, filled_quantity: pos.quantity, avg_price: fillPx, notional: gross, fee }, decisionId);
  }

  /** SELL decision for one lot, with Signal.exit_reason / position_id / price (intended fill) like the backend. */
  private exitDecision(pos: Position, why: ExitReason, fillPx: number, reason: string): Decision {
    const detail = reason; reason = `${why}: ${detail}`; // API.md v0.4: Decision.reason "<EXIT_REASON>: <detail>", Signal.reason the detail
    const id = uid();
    const signal: Signal = { id: uid(), symbol: pos.symbol, action: "SELL", source: this.agentMode ?? "MANUAL", strategy: pos.strategy, reason: detail,
      price: fillPx, exit_reason: why, position_id: pos.id ?? null, regime: this.regime?.regime ?? null, timestamp: this.simNow() };
    const r = this.sellLot(pos, fillPx, id);
    return this.log({ id, symbol: pos.symbol, action: "SELL", strategy: pos.strategy, reason, signal, execution: r });
  }

  private log(d: Omit<Decision, "mode" | "agent_mode"> & { agent_mode?: Decision["agent_mode"] }): Decision {
    const full: Decision = { id: uid(), timestamp: this.simNow(), mode: "SIM", agent_mode: d.agent_mode ?? this.agentMode ?? "MANUAL", regime: this.regime?.regime ?? null, ...d };
    full.explanation ??= full.reason;
    this.decisions.unshift(full);
    this.decisions = this.decisions.slice(0, 200);
    this.emit("DECISION", full);
    return full;
  }

  /** One loop cycle: (advance a bar) -> exits -> entry. Emits PRICE, DECISION, EXECUTION, ALERT, STATUS. */
  private cycle(advance = false): Decision[] {
    const out: Decision[] = [];
    this.loop = { ...this.loop, ticks: this.loop.ticks + 1 };
    if (advance) for (const t of TOKENS) {
      const c = this.candles[t.symbol];
      const live = LIVE_REGIMES[Math.floor(this.loop.ticks / 40 + TOKENS.indexOf(t)) % LIVE_REGIMES.length];
      c.push(appendCandle(c, this.rand, live === "OVERSOLD_PULLBACK" ? "DOWNTREND" : live));
      if (c.length > 600) c.shift();
      this.barsSinceEntry[t.symbol] = (this.barsSinceEntry[t.symbol] ?? 99) + 1;
      const bse = this.portfolio.bars_since_exit ?? {};
      if (bse[t.symbol] != null) bse[t.symbol] += 1;
    }
    const c = this.candles[this.symbol], px = this.last(this.symbol);
    this.regime = this.analyse(this.symbol);
    this.markToMarket();
    this.equityCurve.push({ timestamp: this.simNow(), equity: this.portfolio.equity });
    this.emit("PRICE", { symbol: this.symbol, price: px, candle: c[c.length - 1] });
    const finish = () => { this.syncLoop(); this.emitStatus(); return out; };
    if (!this.agentMode) return finish();

    // 1. protective exits: no confirmation needed, run even while emergency stopped
    for (const p of [...(this.portfolio.positions ?? [])]) {
      const bar = this.candles[p.symbol][this.candles[p.symbol].length - 1];
      const trail = p.id ? this.trails[p.id] : undefined;
      let why: ExitReason | null = null, level = bar.close;
      if (p.trailing_stop_pct && trail != null && bar.low <= trail) { why = "TRAILING_STOP"; level = Math.min(bar.open, trail); }
      else if (p.stop_loss_price && bar.low <= p.stop_loss_price) { why = "STOP_LOSS"; level = Math.min(bar.open, p.stop_loss_price); }
      else if (p.take_profit_price && bar.high >= p.take_profit_price) { why = "TAKE_PROFIT"; level = Math.max(bar.open, p.take_profit_price); }
      if (!why && p.trailing_stop_pct && trail != null && p.id) this.trails[p.id] = Math.max(trail, bar.high * (1 - p.trailing_stop_pct / 100));
      if (why) out.push(this.exitDecision(p, why, level, `lot ${p.id?.slice(0, 6)} filled at ${level.toFixed(2)}`));
    }
    if (this.control.emergency_stopped) {
      // no flatten: the risk layer closes remaining lots on the next bar at that bar's close
      for (const p of [...(this.portfolio.positions ?? [])]) {
        const bar = this.candles[p.symbol][this.candles[p.symbol].length - 1];
        out.push(this.exitDecision(p, "EMERGENCY_STOP", bar.close, `lot ${p.id?.slice(0, 6)} closed at bar close ${bar.close.toFixed(2)}`));
      }
      out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy: this.activeStrategy ?? "-", reason: `${onePrefix("Emergency stop", this.control.reason || "active")}; staying out` }));
      return finish();
    }
    if (this.control.paused) { out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy: this.activeStrategy ?? "-", reason: `Paused: ${this.control.reason ?? "by user"}` })); return finish(); }
    if (this.pending().length) return finish(); // waiting for the user

    // 2. strategy
    let buy = false, reason = "", sizePct = 15, strategy = this.activeStrategy ?? "-", preset: PresetName | null = null;
    const met: string[] = [], failed: string[] = [];
    const open = (this.portfolio.positions ?? []).filter((p) => p.symbol === this.symbol).length;
    if (this.agentMode === "MANUAL" && this.manual) {
      const m = this.manual, cool = (this.portfolio.bars_since_exit ?? {})[this.symbol];
      sizePct = m.risk.position_size_pct;
      if (open >= m.risk.max_open_positions) { failed.push("max_open_positions"); reason = `Max open positions (${m.risk.max_open_positions}) reached`; }
      else if (cool != null && cool <= m.risk.cooldown_bars) { met.push("max_open_positions"); failed.push("cooldown"); reason = `Cooldown: ${cool}/${m.risk.cooldown_bars} bars since last exit`; }
      else {
        const ev = evaluateLast(m, c);
        met.push("max_open_positions", "cooldown", ...ev.met); failed.push(...ev.failed);
        buy = ev.ok;
        const L = (a: string[]) => a.map(conditionLabel).join(", ");
        reason = ev.blocked ? `Entry blocked: ${ev.blocked}` : buy ? `Entry (${m.entry.logic}) met: ${L(ev.met)}` : `Entry (${m.entry.logic}) not met: ${L(ev.failed) || "-"}`;
      }
    } else if (this.agentMode === "AUTO" && this.auto) {
      const style = this.auto.style ?? "BALANCED";
      const sel = this.select(this.regime, style, this.auto.enabled_presets ?? [...PresetNameValues]);
      if (!sel.preset) { strategy = "NONE"; this.activeStrategy = "NONE"; reason = `${sel.reason}. ${this.regime.reason}`; }
      else {
        preset = sel.preset; strategy = sel.preset; this.activeStrategy = sel.preset; sizePct = 10 * (sel.size_multiplier ?? 1);
        if (open >= 3) reason = `${sel.preset}: max exposure (3 entries) reached`;
        else { const w = presetWantsBuy(sel.preset, c, this.barsSinceEntry[this.symbol] ?? 99); buy = w.buy; reason = `${sel.reason}: ${w.reason}`; }
      }
    }
    const signal: Signal = { id: uid(), symbol: this.symbol, action: buy ? "BUY" : "STAY_OUT", source: this.agentMode, strategy, preset, regime: this.regime.regime,
      reason, conditions_met: met, conditions_failed: failed, price: px, proposed_size_pct: sizePct, proposed_notional: this.portfolio.equity * (sizePct / 100), timestamp: this.simNow() };
    if (!buy) { out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason, signal })); return finish(); }

    // 3. risk + execution
    const notional = signal.proposed_notional!;
    if (notional > this.portfolio.cash) {
      out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason: `Risk blocked: insufficient cash for ${notional.toFixed(0)} USDT`, signal,
        risk_verdict: { signal_id: signal.id!, approved: false, reasons: ["insufficient cash"], checks: [{ name: "cash", passed: false }] } }));
      this.alert("INFO", "RISK_BLOCKED", "Entry blocked: insufficient cash", this.symbol);
      return finish();
    }
    const verdict = { signal_id: signal.id!, approved: true, reasons: [], adjusted_size_pct: sizePct, adjusted_notional: notional, requires_confirmation: !!this.control.confirmation_mode,
      checks: [{ name: "paused", passed: true }, { name: "max_exposure", passed: true }, { name: "cash", passed: true }] };
    if (this.control.confirmation_mode) {
      out.push(this.log({ symbol: this.symbol, action: "BUY", strategy, reason: `${reason}. Waiting for your confirmation`, signal, risk_verdict: verdict,
        execution: { client_order_id: uid(), symbol: this.symbol, side: "BUY", status: "PENDING_CONFIRMATION", path: "SWAP", mode: "SIM", notional, avg_price: px } }));
      return finish();
    }
    const id = uid();
    const r = this.buy(this.symbol, notional, strategy, id);
    out.push(this.log({ id, symbol: this.symbol, action: "BUY", strategy, reason, signal, risk_verdict: verdict, execution: r }));
    return finish();
  }
}

/** API.md v0.4 "exactly one prefix": "Paused: lunch", but "Paused by user" stays as is. */
function onePrefix(prefix: string, text?: string | null) {
  const t = (text ?? "").replace(new RegExp(`^(\\s*${prefix.replace(/ /g, "[ _]")}\\s*:\\s*)+`, "i"), "").trim();
  if (!t) return prefix;
  return t.toLowerCase().startsWith(prefix.toLowerCase()) ? t : `${prefix}: ${t}`;
}

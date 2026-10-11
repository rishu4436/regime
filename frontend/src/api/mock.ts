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
import { ApiError, type ActivateManualBody, type ApiClient, type LiveCooldown, type LiveGate, type LiveLimits, type ParseResult, type PresetsTable, type WalletInfo, type WalletConnectStatus, type TradingLimits, type TradingLimitsResponse, type Narrative, type AutoPickResult, type AutoPick, type AutoPickSkip, type WatchlistFields, type WatchlistInfo, type AgentStudioStatus, type X402JobStatus } from "./types";
import { cleanWatchlist, narrativeOf, NARRATIVES, underlyingOf } from "../lib/watchlist";

/** Mock-only demo scenario: "LIVE" pretends to be a LIVE (mainnet) backend with an Agentic Wallet so the LIVE UI can be
 * shown without funds. Nothing leaves the browser; tx hashes are random and labelled as demo. Persisted; switching reloads. */
export type MockScenario = "SIM" | "LIVE";
export const MOCK_SCENARIO: MockScenario = (() => { try { return localStorage.getItem("mock.scenario") === "LIVE" ? "LIVE" : "SIM"; } catch { return "SIM"; } })();
export function setMockScenario(s: MockScenario) { try { localStorage.setItem("mock.scenario", s); } catch { /* private mode */ } window.location.reload(); }
/** Mock-only: in the demo LIVE scenario, force every risk warning + an expired wallet session (for screenshots). */
export const MOCK_WARNINGS: boolean = (() => { try { return localStorage.getItem("mock.warnings") === "1"; } catch { return false; } })();
export function setMockWarnings(on: boolean) { try { localStorage.setItem("mock.warnings", on ? "1" : "0"); } catch { /* private mode */ } window.location.reload(); }
const LIVE = MOCK_SCENARIO === "LIVE";
const MODE = LIVE ? "LIVE" : "SIM";
// Bot 3 LiveRiskProfile defaults (bot3/live): 25/order, 25 daily loss (auto e-stop), 100/day, 50 open, 1% slippage, 900 s, 0.002 BNB
const LIVE_CAP = 25, LIVE_DAILY_LOSS = 25, LIVE_DAILY_NOTIONAL = 100, LIVE_EXPOSURE = 50, LIVE_GAS_RESERVE = 0.002;
const LIVE_CAPITAL = 60;        // "a few dollars is enough to prove the flow works"
const DEMO_WALLET = "0x7a3fD3b1c0E5aB9e2D41f6C8b0aF2e39C4d1B7e2";
const hex = (n: number) => Array.from({ length: n }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("");
/** SIM routing from Bot 4: Ondo + xStock -> RFQ, bStock -> SWAP (the UI never assumes this for a real backend). */
const pathFor = (symbol: string) => (/B$/.test(symbol) ? "SWAP" : "RFQ") as "SWAP" | "RFQ";

const T = (symbol: string, name: string, token_type: "BSTOCK" | "ONDO" | "XSTOCK", start: number): TokenInfo & { start: number } =>
  ({ symbol, name, token_type, chain: "BSC", underlying: underlyingOf(symbol), decimals: token_type === "XSTOCK" ? 8 : 18, start });
// Example token list (Bot 4's real list comes from the Binance RWA search): bStocks, Ondo and xStocks on BSC.
const TOKEN_LIST = [
  T("NVDAB", "NVIDIA BStock", "BSTOCK", 140), T("NVDAon", "NVIDIA (Ondo tokenized)", "ONDO", 140), T("TSLAx", "Tesla xStock", "XSTOCK", 200), T("AAPLB", "Apple BStock", "BSTOCK", 170),
  T("TSLAB", "Tesla BStock", "BSTOCK", 210), T("MSFTB", "Microsoft BStock", "BSTOCK", 420), T("GOOGLB", "Alphabet BStock", "BSTOCK", 165), T("AMZNB", "Amazon BStock", "BSTOCK", 185),
  T("METAB", "Meta BStock", "BSTOCK", 560), T("COINB", "Coinbase BStock", "BSTOCK", 230), T("TSLAon", "Tesla (Ondo tokenized)", "ONDO", 210), T("AAPLon", "Apple (Ondo tokenized)", "ONDO", 170),
  T("SPYon", "S&P 500 ETF (Ondo tokenized)", "ONDO", 560), T("QQQon", "Nasdaq 100 ETF (Ondo tokenized)", "ONDO", 480), T("MSTRon", "MicroStrategy (Ondo tokenized)", "ONDO", 340),
  T("NVDAx", "NVIDIA xStock", "XSTOCK", 140),
];
const TOKENS: TokenInfo[] = TOKEN_LIST.map(({ start: _s, ...t }) => t);
const LIMIT_DEFAULTS: TradingLimits = { order_mode: "strategy", order_usdt: 25, order_pct: 2, max_order_usdt: 25, max_total_usdt: 100, max_per_token_usdt: 50, daily_loss_usdt: 10 };
const WALLET_DAILY_LIMIT = 500;
const START: Record<string, number> = Object.fromEntries(TOKEN_LIST.map((t) => [t.symbol, t.start]));
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
  private control: ControlState = { paused: false, emergency_stopped: false, confirmation_mode: false, trading_mode: MODE, reason: null, updated_at: now() };
  private portfolio: Portfolio = { starting_capital: LIVE ? LIVE_CAPITAL : DEFAULT_INITIAL_CAPITAL, cash: LIVE ? LIVE_CAPITAL : DEFAULT_INITIAL_CAPITAL, positions: [], realized_pnl: 0, equity: LIVE ? LIVE_CAPITAL : DEFAULT_INITIAL_CAPITAL, bars_since_exit: {}, trades_today: 0, daily_pnl: 0, mode: MODE, updated_at: now() };
  private agentMode: "MANUAL" | "AUTO" | null = null;
  private manual: FullStrategy | null = null;
  private auto: AutoConfig | null = null;
  private symbol = "NVDAB";
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
    if (LIVE) { // the LIVE demo starts like the real thing: an autonomous auto agent (AUTO_EXECUTE), loop running 24/7
      this.auto = { symbol: "NVDAB", style: "ACTIVE", capital: LIVE_CAPITAL, activation: "AUTO_EXECUTE", timeframe: "1h", enabled_presets: [...PresetNameValues] };
      this.symbol = "NVDAB"; this.regime = this.analyse(this.symbol);
      this.agentMode = "AUTO"; this.activeStrategy = this.select(this.regime, "ACTIVE", [...PresetNameValues]).preset ?? "NONE";
      this.control = { ...this.control, confirmation_mode: false };
      this.loop = { ...this.loop, running: true };
      this.syncLoop();
    }
  }

  // ------------------------------------------------------------------ system & market data
  health() { return ok({ status: "ok", contracts_version: CONTRACTS_VERSION, trading_mode: MODE }); }
  system() {
    return ok<SystemInfo & { agent_identity: unknown }>({ trading_mode: MODE, live_enabled: LIVE, contracts_version: CONTRACTS_VERSION, sim_seed: LIVE ? null : 42,
      agent_identity: { erc8004_id: "0 (placeholder)", registry: "ERC-8004 Identity Registry (placeholder)", studio_version: "mock", network: "BSC Testnet (placeholder)", explorer_url: "https://8004scan.io" },
      components: { engine: "mock:browser", risk: "mock:browser", exchange: LIVE ? "mock:LiveBinanceClient(agentic_wallet, demo)" : "mock:browser", explainer: "mock:template" } });
  }
  // ------------------------------------------------------------------ live / wallet (requested endpoints, mocked)
  private bnb = 0.0046;          // demo gas balance; each fill burns a little so the low-gas warning shows up
  private dailyNotional = 0;
  private sessionExpired = LIVE && MOCK_WARNINGS; // demo: the Agentic Wallet sign-in expired until "Reconnect wallet"
  private hb = { started_at: now() as string | null, last_tick_at: null as string | null };
  private liveLimits(): LiveLimits {
    const exposure = (this.portfolio.positions ?? []).reduce((a, p) => a + p.quantity * (p.current_price ?? p.avg_entry_price), 0);
    const loss = Math.max(0, -(this.portfolio.daily_pnl ?? 0) - (this.portfolio.positions ?? []).reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0));
    return { live_allowed: true, per_order_cap_usdt: LIVE_CAP, daily_loss_cap_usdt: LIVE_DAILY_LOSS, daily_notional_cap_usdt: LIVE_DAILY_NOTIONAL,
      daily_limit_usdt: LIVE_DAILY_NOTIONAL, max_exposure_usdt: LIVE_EXPOSURE, max_slippage_pct: 1, max_favorable_impact_pct: 3, stale_after_s: 900, bnb_gas_reserve: LIVE_GAS_RESERVE,
      fee_buffer_usdt: 1, min_wallet_usdt: 70, min_order_usdt: 10, market_hours_utc: null, daily_used_usdt: this.dailyNotional, daily_loss_usdt: loss, open_exposure_usdt: exposure,
      daily_remaining_usdt: Math.max(0, LIVE_DAILY_NOTIONAL - this.dailyNotional), warnings: [
        ...(MOCK_WARNINGS || exposure > Math.max(0, LIVE_DAILY_NOTIONAL - this.dailyNotional) ? [{ code: "exit_quota_shortfall", message: `Exit reserve: ${Math.max(0, LIVE_DAILY_NOTIONAL - this.dailyNotional - (MOCK_WARNINGS ? 92 : 0)).toFixed(2)} USDT of daily quota left can't cover selling all open positions (${Math.max(exposure, MOCK_WARNINGS ? 24.5 : 0).toFixed(2)} USDT)` }] : []),
        ...(MOCK_WARNINGS ? ["wallet_too_small", { code: "wallet_limits_stale", message: "Wallet limits last refreshed 25 min ago" }] : []),
      ], limit_source: { per_order_cap_usdt: "risk", daily_limit_usdt: "risk" },
      allowed_tokens: "ALL", tradable_tokens: TOKENS.filter((t) => !/x$/.test(t.symbol)).map((t) => t.symbol), wallet: { daily_limit_usdt: 200, per_order_cap_usdt: 50, allowed_tokens: null } };
  }
  /** demo cooldown (API.md v0.7 shape): an Ondo token cooling down after an exchange off-hours error (40367) */
  private mockCooldowns: LiveCooldown[] = [{ symbol: "NVDAon", code: 40367, until: new Date(Date.now() + 20 * 60_000).toISOString(), reason: "BUY not executed: NVDAon cooling down after exchange error 40367" }];
  private gate(): LiveGate {
    const reasons: string[] = [];
    if (this.control.emergency_stopped) reasons.push(onePrefix("Emergency stop", this.control.reason || "active"));
    else if (this.control.paused) reasons.push(onePrefix("Paused", this.control.reason));
    if (this.sessionExpired) reasons.push("wallet_not_ready: Agentic Wallet session expired, sign in again with the QR code");
    if (this.bnb < LIVE_GAS_RESERVE) reasons.push(`BNB ${this.bnb.toFixed(4)} below the gas reserve ${LIVE_GAS_RESERVE}`);
    if (this.dailyNotional >= LIVE_DAILY_NOTIONAL) reasons.push(`Daily LIVE notional ${this.dailyNotional.toFixed(2)} of ${LIVE_DAILY_NOTIONAL} USDT used`);
    const t = Date.now();
    const cooldowns = this.mockCooldowns.filter((c) => Date.parse(c.until) > t);
    const chain_pending = (this.portfolio.positions ?? []).filter((p) => (p as { pending?: boolean }).pending).map((p) => {
      const q = p as typeof p & { client_order_id?: string | null; held_exit?: { code?: string } | null };
      return { order_id: null, client_order_id: q.client_order_id ?? null, symbol: p.symbol, notional: p.quantity * p.avg_entry_price, tx_hash: null, since: p.opened_at, held_exit: q.held_exit ?? null };
    });
    return { live_trading_enabled: true, binance_live_enabled: true, risk_allow_live: true, wallet_connected: !this.sessionExpired, can_trade: reasons.length === 0, blocked_reasons: reasons, cooldowns, chain_pending };
  }
  getLiveGate() { return ok<LiveGate | null>(LIVE ? this.gate() : null); }
  getWallet() {
    if (!LIVE) return ok<WalletInfo | null>(null);
    this.markToMarket();
    const held = new Map<string, { q: number; px: number }>();
    for (const p of this.portfolio.positions ?? []) { const h = held.get(p.symbol) ?? { q: 0, px: p.current_price ?? p.avg_entry_price }; h.q += p.quantity; held.set(p.symbol, h); }
    return ok<WalletInfo | null>({ provider: "baw", address: DEMO_WALLET, chain: "BSC", chain_id: 56, connected: !this.sessionExpired, session_expired: this.sessionExpired,
      ...(MOCK_WARNINGS ? { stale: true, age_s: 1500 } : {}),
      balances: [
        { asset: "USDT", amount: this.portfolio.cash, value_usdt: this.portfolio.cash, contract_address: "0x55d398326f99059fF775485246999027B3197955" },
        { asset: "BNB", amount: this.bnb, value_usdt: this.bnb * 590 },
        ...[...held].map(([sym, h]) => ({ asset: sym, amount: h.q, value_usdt: h.q * h.px, contract_address: TOKENS.find((t) => t.symbol === sym)?.contract_address ?? null })),
      ],
      bnb_gas_reserve: LIVE_GAS_RESERVE, limits: this.liveLimits(), updated_at: now(), live_gate: this.gate() });
  }
  // mock QR sign-in: "scanned" ~6 s after the QR is shown
  private connectSessions = new Map<string, number>();
  startWalletConnect() {
    const id = `mock-${Date.now().toString(36)}`; this.connectSessions.set(id, Date.now());
    return ok({ qr_url: `https://example.invalid/mock-agentic-wallet-login?session=${id}`, session_id: id, expires_at: new Date(Date.now() + 120_000).toISOString() }, 300);
  }
  getWalletConnect(session_id: string) {
    const t0 = this.connectSessions.get(session_id);
    if (t0 == null) return fail(404, `unknown session ${session_id}`);
    const age = Date.now() - t0;
    return ok<WalletConnectStatus>(age > 120_000 ? { status: "expired" } : age > 6000 ? ((this.sessionExpired = false), { status: "connected", address: DEMO_WALLET, wallet_changed: false }) : { status: "pending" }, 150);
  }
  // Trading limits (mock of Bot 6's GET/PUT /api/settings/limits): saved in localStorage like logs/limits.json
  private limits: TradingLimits = (() => { try { const r = localStorage.getItem("mock.limits"); if (r) return { ...LIMIT_DEFAULTS, ...JSON.parse(r) }; } catch { /* ignore */ } return { ...LIMIT_DEFAULTS }; })();
  getLimits() { return ok<TradingLimitsResponse | null>({ ...this.limits, wallet_daily_limit_usdt: WALLET_DAILY_LIMIT, defaults: { ...LIMIT_DEFAULTS }, updated_at: now() }); }
  putLimits(part: Partial<TradingLimits>) {
    const b: TradingLimits = { ...this.limits, ...part };
    const nums: (keyof TradingLimits)[] = ["order_usdt", "order_pct", "max_order_usdt", "max_total_usdt", "max_per_token_usdt", "daily_loss_usdt"];
    if (!["strategy", "fixed", "pct"].includes(b.order_mode)) return fail(422, "order_mode must be strategy, fixed or pct");
    for (const k of nums.filter((k) => b.order_mode !== "strategy" || (k !== "order_usdt" && k !== "order_pct"))) if (!(typeof b[k] === "number" && (b[k] as number) > 0)) return fail(422, `${k} must be greater than 0`);
    if (b.order_pct > 100) return fail(422, "order_pct can't be above 100");
    if (b.order_mode === "fixed" && b.order_usdt > b.max_order_usdt) return fail(422, "order size is above the max per order");
    if (b.max_order_usdt > b.max_total_usdt) return fail(422, "max per order can't be above max total invested");
    if (b.max_per_token_usdt > b.max_total_usdt) return fail(422, "max in one token can't be above max total invested");
    for (const k of ["max_order_usdt", "max_total_usdt", "max_per_token_usdt", "daily_loss_usdt"] as const)
      if (b[k] > WALLET_DAILY_LIMIT) return fail(422, `${k} (${b[k]}) is above the wallet's own daily limit of ${WALLET_DAILY_LIMIT} USDT`);
    this.limits = { order_mode: b.order_mode, order_usdt: b.order_usdt, order_pct: b.order_pct, max_order_usdt: b.max_order_usdt, max_total_usdt: b.max_total_usdt, max_per_token_usdt: b.max_per_token_usdt, daily_loss_usdt: b.daily_loss_usdt };
    try { localStorage.setItem("mock.limits", JSON.stringify(this.limits)); } catch { /* ignore */ }
    return this.getLimits() as Promise<TradingLimitsResponse>;
  }
  // mock Agent Studio: create key -> "funded" ~8 s later -> register
  private studio: AgentStudioStatus & { keyAt?: number } = { registered: false, network: "mainnet", has_key: false, identity_address: null, bnb_balance: null, bnb_needed: 0.002 };
  getAgentStudio() {
    const st = { ...this.studio };
    if (st.keyAt && !st.registered) st.bnb_balance = Date.now() - st.keyAt > 8000 ? 0.0025 : 0;
    delete st.keyAt; return ok<AgentStudioStatus | null>(st, 150);
  }
  createAgentStudioKey() {
    this.studio = { ...this.studio, has_key: true, identity_address: "0x4e1D2c9A7b3F0e8D6a5C4b3A2f1E0d9C8b7A6f51", bnb_balance: 0, keyAt: Date.now() };
    return ok({ identity_address: this.studio.identity_address! }, 400);
  }
  registerAgentStudio(network: string) {
    if (!this.studio.has_key) return fail(409, "Create the identity wallet first");
    if (this.studio.keyAt && Date.now() - this.studio.keyAt < 8000) return fail(409, "Identity wallet needs about 0.002 BNB for gas first");
    this.studio = { ...this.studio, registered: true, network, erc8004_id: "1234 (mock)", explorer_url: "https://8004scan.io", bnb_balance: 0.0013 };
    return ok({ erc8004_id: "1234 (mock)", explorer_url: "https://8004scan.io", tx_hash: null }, 1500);
  }
  // mock x402: report ready ~4 s after paying
  private x402Jobs = new Map<string, { at: number; symbol: string }>();
  private x402Calls = 0;
  getX402Price(_symbol: string) { return ok({ enabled: true, price_usdt: 0.1, cap_usdt: 0.1, calls_left_today: Math.max(0, 5 - this.x402Calls), quota_usdt_left: 1.0 - this.x402Calls * 0.1 }, 120); }
  startX402Analysis(symbol: string) {
    if (this.x402Calls >= 5) return fail(429, "Daily x402 call cap reached");
    this.x402Calls += 1; const id = `x402-${Date.now().toString(36)}`; this.x402Jobs.set(id, { at: Date.now(), symbol });
    return ok({ job_id: id, paid_usdt: 0.1, tx_hash: null }, 600);
  }
  getX402Job(job_id: string) {
    const j = this.x402Jobs.get(job_id);
    if (!j) return fail(404, `unknown job ${job_id}`);
    return ok<X402JobStatus>(Date.now() - j.at < 4000 ? { status: "pending" } : { status: "done", report: `Example report (mock data) for ${j.symbol}.\n\nThis is placeholder text; with a real backend the report comes from Binance Stock Agent.` }, 150);
  }
  reconcileWallet() { return ok({ address: DEMO_WALLET, was_blocked: false, kept: [], dropped: [], wallet_blocked: false }, 300); }
  getTokens(q = "") {
    const ql = q.toLowerCase();
    return ok(TOKENS.filter((t) => !ql || t.symbol.toLowerCase().includes(ql) || t.name.toLowerCase().includes(ql))
      .map((t) => { const st = tokenStatus(t.symbol), vol = this.candles[t.symbol].slice(-24).reduce((a, c) => a + c.volume * c.close, 0);
        return { ...t, type: t.token_type, token_status: st, status: st.trading_status, tradable: st.tradable, min_order_usdt: st.min_order_usdt ?? 10, max_order_usdt: null,
          price: this.last(t.symbol), volume_24h: vol, liquidity: st.trading_status === "NO_LIQUIDITY" ? 0 : Math.round(vol * 0.6) }; }));
  }
  /** Example Auto-pick ranking (the real one is Bot 2's engine.autopick.rank_tokens): narrative filter, then momentum/trend/liquidity. */
  private rank(narrative: Narrative, max: number): AutoPickResult {
    const picks: (AutoPick & { score: number })[] = [], skipped: AutoPickSkip[] = [];
    const label = (n: Narrative | null) => NARRATIVES.find((x) => x.value === n)?.label ?? null;
    for (const t of TOKENS) {
      const n = narrativeOf(t.symbol);
      if (narrative !== "ANY" && n !== narrative) continue;
      const st = tokenStatus(t.symbol);
      if (st.trading_status === "NO_LIQUIDITY") { skipped.push({ symbol: t.symbol, reason: "No liquidity on BNB Chain" }); continue; }
      if (st.trading_status === "OFFHOURS") { skipped.push({ symbol: t.symbol, reason: "Market closed right now" }); continue; }
      const c = this.candles[t.symbol], px = c[c.length - 1].close, ch = (px / c[Math.max(0, c.length - 21)].close - 1) * 100;
      const r = this.analyse(t.symbol).regime;
      const trend = r === "STRONG_UPTREND" ? 1 : r === "MILD_UPTREND" ? 0.75 : r === "OVERSOLD_PULLBACK" ? 0.55 : r === "SIDEWAYS" ? 0.4 : 0.1;
      const score = Math.round(Math.max(0, Math.min(100, 45 * trend + 35 * Math.max(0, Math.min(1, (ch + 5) / 15)) + 20 * (t.token_type === "BSTOCK" ? 1 : 0.7))));
      const rl = r.toLowerCase().replace(/_/g, " ");
      picks.push({ symbol: t.symbol, narrative: label(n), score,
        reason: `${ch >= 0 ? "Up" : "Down"} ${Math.abs(ch).toFixed(1)}% over 20 bars, in a ${rl}${t.token_type === "BSTOCK" ? ", trades 24/7" : ""}` });
    }
    picks.sort((a, b) => b.score - a.score);
    for (const p of picks.slice(max)) skipped.push({ symbol: p.symbol, reason: `Ranked lower (score ${p.score})` });
    return { picks: picks.slice(0, max), skipped, example: true };
  }
  getAutoPick(narrative: Narrative, max_tokens: number) { return ok<AutoPickResult | null>(this.rank(narrative, Math.max(1, Math.min(5, max_tokens))), 200); }
  private watch: WatchlistFields = {};
  private watchSyms(): string[] {
    const ap = this.watch.auto_pick;
    return ap?.enabled ? this.rank(ap.narrative, ap.max_tokens).picks.map((p) => p.symbol) : cleanWatchlist(this.watch.watchlist, this.symbol);
  }
  getWatchlist() { return ok<WatchlistInfo | null>(this.watchInfo()); }
  private watchInfo(): WatchlistInfo {
    const ap = this.watch.auto_pick, ranked = ap?.enabled ? this.rank(ap.narrative, ap.max_tokens) : null;
    const syms = this.agentMode ? this.watchSyms() : [];
    return { watchlist: syms.map((sym) => {
      const r = this.analyse(sym).regime, held = (this.portfolio.positions ?? []).filter((p) => p.symbol === sym);
      const last = this.decisions.find((d) => d.symbol === sym);
      return { symbol: sym, regime: r, action: last?.action ?? null, held: held.length > 0, position_usdt: held.reduce((a, p) => a + p.quantity * (p.current_price ?? p.avg_entry_price), 0) || null };
    }), picks: ranked?.picks ?? [], skipped: ranked?.skipped ?? [], auto_pick: ap ?? null, updated_at: now() };
  }
  getCandles(symbol: string, timeframe: Timeframe = "1h", limit = 200, opts?: { history?: boolean }) {
    if (!this.candles[symbol]) return fail(404, `unknown symbol ${symbol}`);
    // history=true: the full seeded series the mock backtest runs on; default: the live loop's bars up to now
    if (opts?.history) return ok(generateMixedCandles(symbol, START[symbol] ?? 100, limit, timeframe));
    return ok(this.candles[symbol].slice(-limit));
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
  backtestCandles(symbol: string, timeframe: Timeframe = "1h", limit = 500) { return this.getCandles(symbol, timeframe, limit, { history: true }); }

  // ------------------------------------------------------------------ agent & loop
  activateManual(body: ActivateManualBody) {
    let src: unknown;
    if ("strategy_id" in body) { src = this.strategies.get(body.strategy_id); if (!src) return fail(404, `strategy ${body.strategy_id} not found`); }
    else src = body.strategy;
    const v = this.validate(src); if (typeof v === "string") return fail(422, v);
    this.manual = fillStrategy(v); this.auto = null; this.agentMode = "MANUAL"; this.symbol = this.manual.symbol;
    this.watch = { watchlist: (src as WatchlistFields).watchlist, auto_pick: (src as WatchlistFields).auto_pick };
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
    this.watch = { watchlist: (c as WatchlistFields).watchlist, auto_pick: (c as WatchlistFields).auto_pick };
    this.regime = this.analyse(c.symbol);
    this.activeStrategy = this.select(this.regime, c.style ?? "BALANCED", c.enabled_presets ?? [...PresetNameValues]).preset ?? "NONE";
    this.setControl({ confirmation_mode: c.activation === "CONFIRMATION" });
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
  getLoop() { return ok({ ...this.loop, ...this.hb, next_tick_at: this.loop.running && this.hb.last_tick_at ? new Date(Date.parse(this.hb.last_tick_at) + this.loop.interval_seconds * 1000).toISOString() : null }); }
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
    if (!("entry" in out || "exit" in out)) return ok<ParseResult>({ kind: "AUTO", config: out as AutoConfig, notes: [] }, 450);
    const m = out as ManualStrategy, e = m.entry, notes: string[] = [];
    // mirror Bot 5: no entry rule in the text -> team default "price above EMA(20)", with a note saying so
    if (e && ![e.rsi_below, e.price_above_sma, e.price_above_ema, e.macd_bullish_cross, e.bollinger_lower, e.volume_above_avg, e.breakout].some((c) => c?.enabled)) {
      e.price_above_ema = { ...e.price_above_ema, enabled: true, period: 20 };
      notes.push("No entry rule found in your text, so the entry is set to the team default: buy when price is above EMA(20).");
    }
    return ok<ParseResult>({ kind: "MANUAL", strategy: m, notes }, 450);
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
      recent_decisions: this.decisions.slice(0, 50), timestamp: now(),
      ...(this.agentMode && this.watchSyms().length > 1 ? { watchlist: this.watchSyms() } : {}) } as StatusSnapshot;
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
    return { order_id: uid(), client_order_id: uid(), symbol, side, status: "FILLED", path: LIVE ? pathFor(symbol) : "SWAP", mode: MODE, timestamp: now() };
  }
  private done(r: ExecutionResult, decisionId?: string) {
    if (LIVE && r.status === "FILLED") { // demo hash: not on chain
      const tx = "0x" + hex(64);
      r = { ...r, tx_hash: tx, explorer_url: `https://bscscan.com/tx/${tx}`, route: r.path, quote_expires_at: new Date(Date.now() + 30_000).toISOString(), real_funds: true, price_impact_pct: +(0.05 + Math.random() * 0.35).toFixed(2) } as ExecutionResult;
      this.bnb = Math.max(0, this.bnb - 0.00025);
      if (r.side === "BUY") this.dailyNotional += r.notional ?? 0;
    }
    this.emit("EXECUTION", { ...r, decision_id: decisionId ?? null }); return r;
  }

  /** Open a new lot (several lots per symbol are allowed; each has a stable Position.id). */
  private buy(symbol: string, notional: number, strategy: string, decisionId?: string): ExecutionResult {
    const px = this.last(symbol), pf = this.portfolio, ps = pf.positions ?? (pf.positions = []), base = this.exec(symbol, "BUY");
    notional = Math.min(notional, pf.cash);
    if (notional <= 1) return this.done({ ...base, status: "REJECTED", error: "insufficient cash" }, decisionId);
    if (LIVE && notional > LIVE_CAP + 1e-9) return this.done({ ...base, status: "REJECTED", error: `notional ${notional.toFixed(2)} exceeds BINANCE_LIVE_MAX_NOTIONAL=${LIVE_CAP.toFixed(2)}` }, decisionId);
    const q = (notional * (1 - FEE)) / px, x = this.manual?.exit;
    const pos: Position = { id: uid(), symbol, quantity: q, avg_entry_price: px, opened_at: now(), strategy, current_price: px, highest_price: px,
      stop_loss_price: px * (1 - (x?.stop_loss_pct ?? 6) / 100), take_profit_price: px * (1 + (x?.take_profit_pct ?? 12) / 100) };
    if (x?.trailing_stop.enabled) { pos.trailing_stop_pct = x.trailing_stop.pct; this.trails[pos.id!] = px * (1 - x.trailing_stop.pct / 100); }
    if (LIVE) Object.assign(pos, { pending: true, client_order_id: base.client_order_id, pendingTick: this.loop.ticks });
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
    const full: Decision = { id: uid(), timestamp: this.simNow(), mode: MODE, agent_mode: d.agent_mode ?? this.agentMode ?? "MANUAL", regime: this.regime?.regime ?? null, ...d };
    full.explanation ??= full.reason;
    this.decisions.unshift(full);
    this.decisions = this.decisions.slice(0, 200);
    this.emit("DECISION", full);
    return full;
  }

  /** One loop cycle: (advance a bar) -> exits -> entry. Emits PRICE, DECISION, EXECUTION, ALERT, STATUS. */
  private cycle(advance = false): Decision[] {
    const out: Decision[] = [];
    this.loop = { ...this.loop, ticks: this.loop.ticks + 1 }; this.hb.last_tick_at = now();
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
    if (LIVE && !this.control.emergency_stopped && (this.liveLimits().daily_loss_usdt ?? 0) >= LIVE_DAILY_LOSS) {
      const c = this.setControl({ emergency_stopped: true, reason: `Daily loss cap hit (${LIVE_DAILY_LOSS} USDT), stopped automatically` });
      this.alert("CRITICAL", "EMERGENCY_STOP", onePrefix("Emergency stop", c.reason));
    }

    // 1. protective exits: no confirmation needed, run even while emergency stopped
    // LIVE demo: a fresh buy is "settling on-chain" for one tick; its exits wait until it fills
    for (const p of this.portfolio.positions ?? []) if ((p as { pending?: boolean }).pending === true) {
      const age = this.loop.ticks - ((p as { pendingTick?: number }).pendingTick ?? 0);
      if (age >= 2) Object.assign(p, { pending: false, client_order_id: null, held_exit: null, pendingTick: undefined });
    }
    for (const p of [...(this.portfolio.positions ?? [])]) {
      if ((p as { pending?: boolean }).pending) {
        (p as { held_exit?: unknown }).held_exit = { code: "pending_exit", reason: "buy still settling on-chain", source: "mock" };
        out.push(this.log({ symbol: p.symbol, action: "STAY_OUT", strategy: p.strategy ?? "-", reason: `Exit check held for lot ${p.id?.slice(0, 6)}: buy still settling on-chain` }));
        continue;
      }
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
    let notional = signal.proposed_notional!;
    const liveChecks: { name: string; passed: boolean; detail?: string }[] = [];
    if (LIVE) { // Bot 3 LIVE hard caps (names as in backend/risk on bot3/live)
      const L = this.liveLimits();
      if (notional > LIVE_CAP) { liveChecks.push({ name: "live_order_cap", passed: true, detail: `Order reduced from ${notional.toFixed(2)} to ${LIVE_CAP.toFixed(2)} USDT (per-order cap)` }); notional = LIVE_CAP; }
      else liveChecks.push({ name: "live_order_cap", passed: true, detail: `${notional.toFixed(2)} USDT within the ${LIVE_CAP} USDT per-order cap` });
      liveChecks.push({ name: "live_daily_notional", passed: (L.daily_used_usdt ?? 0) + notional <= LIVE_DAILY_NOTIONAL, detail: `Traded today ${(L.daily_used_usdt ?? 0).toFixed(2)} + ${notional.toFixed(2)} vs cap ${LIVE_DAILY_NOTIONAL} USDT` });
      liveChecks.push({ name: "live_max_exposure", passed: (L.open_exposure_usdt ?? 0) + notional <= LIVE_EXPOSURE, detail: `Open ${(L.open_exposure_usdt ?? 0).toFixed(2)} + ${notional.toFixed(2)} vs cap ${LIVE_EXPOSURE} USDT` });
      liveChecks.push({ name: "live_gas_reserve", passed: this.bnb >= LIVE_GAS_RESERVE, detail: `BNB ${this.bnb.toFixed(4)}, reserve ${LIVE_GAS_RESERVE}` });
      const failed = liveChecks.filter((c) => !c.passed);
      if (failed.length) {
        const why = failed.map((c) => `Blocked (${c.name}): ${c.detail}`).join("; ");
        out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason: `BUY not executed: ${why}`, signal,
          risk_verdict: { signal_id: signal.id!, approved: false, reasons: failed.map((c) => c.detail ?? c.name), checks: liveChecks } }));
        this.alert("INFO", "RISK_BLOCKED", `BUY ${this.symbol} not executed: ${why}`, this.symbol);
        return finish();
      }
    }
    if (notional > this.portfolio.cash) {
      out.push(this.log({ symbol: this.symbol, action: "STAY_OUT", strategy, reason: `Risk blocked: insufficient cash for ${notional.toFixed(0)} USDT`, signal,
        risk_verdict: { signal_id: signal.id!, approved: false, reasons: ["insufficient cash"], checks: [{ name: "cash", passed: false }] } }));
      this.alert("INFO", "RISK_BLOCKED", "Entry blocked: insufficient cash", this.symbol);
      return finish();
    }
    const verdict = { signal_id: signal.id!, approved: true, reasons: [], adjusted_size_pct: sizePct, adjusted_notional: notional, requires_confirmation: !!this.control.confirmation_mode,
      checks: [{ name: "paused", passed: true }, { name: "max_exposure", passed: true }, { name: "cash", passed: true }, ...liveChecks] };
    if (this.control.confirmation_mode) {
      out.push(this.log({ symbol: this.symbol, action: "BUY", strategy, reason: `${reason}. Waiting for your confirmation`, signal, risk_verdict: verdict,
        execution: { client_order_id: uid(), symbol: this.symbol, side: "BUY", status: "PENDING_CONFIRMATION", path: LIVE ? pathFor(this.symbol) : "SWAP", mode: MODE, notional, avg_price: px } }));
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

/** Mock token_status (shape requested from Bot 6): bStock 24/7, Ondo off-hours outside US hours (20 USD min), xStock no liquidity. */
function tokenStatus(sym: string) {
  if (/x$/.test(sym)) return { trading_status: "NO_LIQUIDITY", tradable: false, min_order_usdt: null, next_open_time: null };
  if (/on$/.test(sym)) {
    const d = new Date(), h = d.getUTCHours() + d.getUTCMinutes() / 60, wk = d.getUTCDay() % 6 !== 0, open = wk && h >= 13.5 && h < 20;
    const next = new Date(d); next.setUTCHours(13, 30, 0, 0); if (next <= d || !wk) do next.setUTCDate(next.getUTCDate() + 1); while (next.getUTCDay() % 6 === 0);
    return { trading_status: open ? "TRADING" : "OFFHOURS", tradable: true, min_order_usdt: 20, next_open_time: open ? null : next.toISOString() };
  }
  return { trading_status: "TRADING", tradable: true, min_order_usdt: null, next_open_time: null };
}

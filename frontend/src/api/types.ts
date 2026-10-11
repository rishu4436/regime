// The typed API surface the UI uses, mirroring backend/app/API.md (Bot 6) exactly.
// Two implementations: ./http (FastAPI REST + WS) and ./mock (in-browser, same endpoints & payloads).
import type {
  Alert, AutoConfig, BacktestRequest, BacktestResult, Candle, ControlState, Decision, Event, LoopState, ManualStrategy,
  PerformanceSummary, Portfolio, PresetName, Regime, RegimeAnalysis, StatusSnapshot, StrategySelection, SystemInfo, Timeframe,
  TokenInfo, TraderStyle,
} from "../contracts";

export type { Alert, AlertLevel, BacktestRequest, LoopState, PerformanceSummary, SystemInfo } from "../contracts";

// Contract 0.2.0 models used by the API (Alert, PerformanceSummary, SystemInfo, LoopState, BacktestRequest) come from
// contracts.ts; only the small app-only bodies/responses below live here.
/** GET /api/auto/presets-table (app type). source = "engine:<Class>" or "fallback:<Class>"; preset null = stay out. */
export interface PresetsTableEntry { regime: Regime; style: TraderStyle; preset: PresetName | null; size_multiplier: number; reason: string }
export interface PresetsTable { source: string; entries: PresetsTableEntry[] }
// ---------------------------------------------------------------- LIVE (app types; shapes Rishu posted to Bot 6)
/** Active LIVE caps + usage. Keys = Bot 3 RiskManager.live_limits() (bot3/live 6aa8882) plus the /api/wallet fields. */
export interface LiveLimits {
  live_allowed?: boolean | null;
  per_order_cap_usdt?: number | null;        // 25
  daily_loss_cap_usdt?: number | null;       // 25 (hitting it triggers the emergency stop)
  daily_notional_cap_usdt?: number | null;   // 100 traded per UTC day
  max_exposure_usdt?: number | null;         // 50 open at once
  max_slippage_pct?: number | null;          // 1
  stale_after_s?: number | null;             // 900
  bnb_gas_reserve?: number | null;           // 0.002 BNB
  fee_buffer_usdt?: number | null;
  /** Smallest wallet that can place a live buy: token minimum order + fee buffer (bot3/live-fixes). */
  min_wallet_usdt?: number | null;
  market_hours_utc?: string | null;
  daily_used_usdt?: number | null;           // LIVE BUY notional filled today
  daily_loss_usdt?: number | null;           // today's realized + unrealized loss
  open_exposure_usdt?: number | null;
  daily_limit_usdt?: number | null;          // effective daily limit (stricter of agent and wallet)
  daily_remaining_usdt?: number | null;
  /** Which side set each effective cap (bot3/live 53907b5): "risk" (agent) or "wallet". */
  limit_source?: string | { per_order_cap_usdt?: "risk" | "wallet" | string; daily_limit_usdt?: "risk" | "wallet" | string } | null; // bot6/live may send one string ("app")
  /** Wallet/risk-side restriction only (API v0.7): array of symbols, "ALL" / "LIMITED" (baw wallet) or null. */
  allowed_tokens?: AllowedTokens;
  /** Tokens the agent can be activated on (exchange list minus xStock in LIVE, or LIVE_ALLOWED_TOKENS). */
  tradable_tokens?: string[] | null;
  /** Risk warnings for the UI, e.g. the exit-reserve warning (remaining daily quota can't cover selling all positions). */
  warnings?: string[] | null;
  /** The wallet's own limits (Agentic Wallet / Binance App), if it exposes them: the stricter one applies. */
  wallet?: { per_order_cap_usdt?: number | null; daily_limit_usdt?: number | null; allowed_tokens?: AllowedTokens } | null;
}
export type AllowedTokens = string[] | "ALL" | "LIMITED" | string | null | undefined;
export interface WalletBalance { symbol?: string; asset?: string; amount?: number; quantity?: number; usd_value?: number | null; value_usdt?: number | null; contract_address?: string | null }
/** GET /api/wallet: the connected wallet (Agentic Wallet `baw`) on BNB Smart Chain. */
export interface WalletInfo {
  provider?: string | null;                  // "baw"
  address?: string | null;
  chain?: string | null;                     // "BSC"
  chain_id?: string | number | null;         // 56
  connected?: boolean | null;
  balances: WalletBalance[];
  bnb_gas_reserve?: number | null;
  limits?: LiveLimits | null;
  updated_at?: string | null;
  live_gate?: LiveGate | null;               // accepted here too
  session_expired?: boolean | null;          // Agentic Wallet session needs re-sign-in (QR in the Binance App)
}
/** The live gate: every switch that must be on before a LIVE BUY, and why trading is blocked right now. */
export interface LiveGate {
  live_trading_enabled?: boolean | null;     // env LIVE_TRADING_ENABLED (Bot 6)
  binance_live_enabled?: boolean | null;     // env BINANCE_LIVE_ENABLED (Bot 4)
  risk_allow_live?: boolean | null;          // RiskConfig.allow_live (Bot 3)
  wallet_connected?: boolean | null;         // baw wallet status CONNECTED
  can_trade: boolean;
  blocked_reasons: string[];
  /** API.md v0.7: per-token cooldowns after an exchange error (until = ISO time) */
  cooldowns?: LiveCooldown[] | null;
  /** API.md v0.7: BUYs submitted and still waiting on chain */
  chain_pending?: ChainPending[] | null;
}
export interface LiveCooldown { symbol: string; code?: string | number | null; until: string; reason?: string | null }
export interface ChainPending { order_id?: string | null; client_order_id?: string | null; symbol: string; notional?: number | null; tx_hash?: string | null; since?: string | null; held_exit?: { code?: string } | null }
/** ExecutionResult as the LIVE backend sends it (extra optional fields until they are in the contract). */
export interface LiveExecutionFields { price_impact_pct?: number | null; tx_hash?: string | null; explorer_url?: string | null; route?: string | null; quote_expires_at?: string | null; real_funds?: boolean | null }
/** LoopState extras requested for the 24/7 monitor (optional; the UI falls back to what it observes). */
export interface LoopHeartbeat {
  started_at?: string | null; last_tick_at?: string | null; next_tick_at?: string | null;
  /** bot6/live: GET /api/loop -> heartbeat */
  heartbeat?: { started_at: string; uptime_s: number; last_tick_at?: string | null; seconds_since_tick?: number | null; restarts?: number; last_error?: string | null; autostart?: boolean } | null;
}

export type ParseResult = { kind: "MANUAL"; strategy: ManualStrategy } | { kind: "AUTO"; config: AutoConfig };
export type ActivateManualBody = { strategy_id: string } | { strategy: ManualStrategy };

/** Error from the API: 404 unknown id/symbol, 409 invalid state, 422 validation. */
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`${status === 404 ? "Not found" : status === 409 ? "Not allowed right now" : status === 422 ? "Invalid input" : `Error ${status}`}: ${detail}`);
  }
}

export interface ApiClient {
  readonly kind: "mock" | "http";
  // system
  health(): Promise<{ status: string; contracts_version?: string; trading_mode?: string }>;
  system(): Promise<SystemInfo>;
  // market data
  getTokens(q?: string): Promise<TokenInfo[]>;
  /** Default: SIM candles pinned to the loop's current bar. `history: true` = the full seeded series /api/backtest runs on. */
  getCandles(symbol: string, timeframe?: Timeframe, limit?: number, opts?: { history?: boolean }): Promise<Candle[]>;
  getPrice(symbol: string): Promise<{ symbol: string; price: number }>;
  getRegime(symbol: string, timeframe?: Timeframe): Promise<RegimeAnalysis>;
  getSelection(symbol: string, style?: TraderStyle, timeframe?: Timeframe): Promise<StrategySelection>;
  /** Full (regime x style) -> preset table from the wired engine's select_strategy. */
  getPresetsTable(): Promise<PresetsTable>;
  // manual strategies
  listStrategies(): Promise<ManualStrategy[]>;
  saveStrategy(s: ManualStrategy): Promise<ManualStrategy>;
  getStrategy(id: string): Promise<ManualStrategy>;
  updateStrategy(id: string, s: ManualStrategy): Promise<ManualStrategy>;
  deleteStrategy(id: string): Promise<{ deleted: string }>;
  validateStrategy(json: unknown): Promise<ManualStrategy>;
  backtest(req: BacktestRequest): Promise<BacktestResult>;
  /** Candles for the backtest chart (BacktestResult has none): GET /api/candles with the same limit. */
  /** Candles for the backtest chart: getCandles(..., { history: true }). An older backend ignores the flag (shorter, pinned series). */
  backtestCandles(symbol: string, timeframe?: Timeframe, limit?: number): Promise<Candle[]>;
  // agent & loop
  activateManual(body: ActivateManualBody): Promise<StatusSnapshot>;
  activateAuto(c: AutoConfig): Promise<StatusSnapshot>;
  deactivate(): Promise<StatusSnapshot>;
  getLoop(): Promise<LoopState>;
  startLoop(interval_seconds?: number): Promise<LoopState>;
  stopLoop(): Promise<LoopState>;
  tick(): Promise<Decision[]>;
  // decisions / confirmations / positions
  getStatus(): Promise<StatusSnapshot>;
  getPortfolio(): Promise<Portfolio>;
  getDecisions(limit?: number, symbol?: string): Promise<Decision[]>;
  getDecision(id: string): Promise<Decision>;
  getConfirmations(): Promise<Decision[]>;
  // live / wallet (null = endpoint not available on this backend yet)
  getLiveGate(): Promise<LiveGate | null>;
  getWallet(): Promise<WalletInfo | null>;
  approve(decisionId: string): Promise<Decision>;
  reject(decisionId: string, reason?: string): Promise<Decision>;
  /** Manual override: close ONE lot by Position.id at the last price (signal.exit_reason MANUAL). 404 if not open. */
  closePositionById(positionId: string): Promise<Decision>;
  /** Manual override: close every open lot of a symbol (ExitReason.MANUAL). */
  closePosition(symbol: string): Promise<Decision>;
  // control
  getControl(): Promise<ControlState>;
  pause(reason?: string): Promise<ControlState>;
  /** Clears `paused` only (not an emergency stop). */
  resume(): Promise<ControlState>;
  /** Clears `emergency_stopped` (leaves `paused` unchanged). */
  resetEmergencyStop(): Promise<ControlState>;
  emergencyStop(reason?: string, flatten?: boolean): Promise<ControlState>;
  setConfirmationMode(enabled: boolean): Promise<ControlState>;
  // monitoring
  getAlerts(limit?: number): Promise<Alert[]>;
  getPerformance(): Promise<PerformanceSummary>;
  // llm
  llmParse(text: string, default_symbol?: string): Promise<ParseResult>;
  llmAsk(question: string): Promise<{ answer: string }>;
  llmSummary(): Promise<{ summary: string }>;
  /** WS /ws Event stream. Returns an unsubscribe function. */
  subscribe(onEvent: (e: Event) => void): () => void;
}
export type { PresetName };

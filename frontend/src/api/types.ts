// The typed API surface the UI uses, mirroring backend/app/API.md (Bot 6) exactly.
// Two implementations: ./http (FastAPI REST + WS) and ./mock (in-browser, same endpoints & payloads).
import type {
  Alert, AutoConfig, BacktestRequest, BacktestResult, Candle, ControlState, Decision, Event, LoopState, ManualStrategy,
  PerformanceSummary, Portfolio, PresetName, RegimeAnalysis, StatusSnapshot, StrategySelection, SystemInfo, Timeframe,
  TokenInfo, TraderStyle,
} from "../contracts";

export type { Alert, AlertLevel, BacktestRequest, LoopState, PerformanceSummary, SystemInfo } from "../contracts";

// Contract 0.2.0 models used by the API (Alert, PerformanceSummary, SystemInfo, LoopState, BacktestRequest) come from
// contracts.ts; only the small app-only bodies/responses below live here.
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
  getCandles(symbol: string, timeframe?: Timeframe, limit?: number): Promise<Candle[]>;
  getPrice(symbol: string): Promise<{ symbol: string; price: number }>;
  getRegime(symbol: string, timeframe?: Timeframe): Promise<RegimeAnalysis>;
  getSelection(symbol: string, style?: TraderStyle, timeframe?: Timeframe): Promise<StrategySelection>;
  // manual strategies
  listStrategies(): Promise<ManualStrategy[]>;
  saveStrategy(s: ManualStrategy): Promise<ManualStrategy>;
  getStrategy(id: string): Promise<ManualStrategy>;
  updateStrategy(id: string, s: ManualStrategy): Promise<ManualStrategy>;
  deleteStrategy(id: string): Promise<{ deleted: string }>;
  validateStrategy(json: unknown): Promise<ManualStrategy>;
  backtest(req: BacktestRequest): Promise<BacktestResult>;
  /** Candles for the backtest chart (BacktestResult has none): GET /api/candles with the same limit. */
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
  approve(decisionId: string): Promise<Decision>;
  reject(decisionId: string, reason?: string): Promise<Decision>;
  /** Manual override: close the open position(s) of a symbol at market (ExitReason.MANUAL). API closes by symbol. */
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

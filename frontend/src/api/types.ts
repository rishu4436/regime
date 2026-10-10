// The typed API surface the UI uses. Two implementations: ./http (Bot 6's FastAPI REST + WS) and ./mock.
import type {
  AutoConfig, BacktestResult, Candle, ControlState, Decision, Event, ExecutionResult, ManualStrategy, OrderRequest,
  StatusSnapshot, Timeframe, TokenInfo,
} from "../contracts";

/** Body of POST /api/backtest. NOTE: not (yet) a contract model - requested from the coordinator. */
export interface BacktestRequest { strategy: ManualStrategy; initial_capital: number; limit?: number }

export interface ApiClient {
  readonly kind: "mock" | "http";
  health(): Promise<{ status: string; trading_mode?: string }>;
  getStatus(): Promise<StatusSnapshot>;
  getTokens(q?: string): Promise<TokenInfo[]>;
  getCandles(symbol: string, timeframe?: Timeframe, limit?: number): Promise<Candle[]>;
  // strategy
  activateManual(strategy: ManualStrategy): Promise<StatusSnapshot>;
  activateAuto(config: AutoConfig): Promise<StatusSnapshot>;
  backtest(req: BacktestRequest): Promise<BacktestResult>;
  // controls
  pause(reason?: string): Promise<ControlState>;
  resume(): Promise<ControlState>;
  emergencyStop(reason?: string): Promise<ControlState>;
  setConfirmationMode(enabled: boolean): Promise<ControlState>;
  confirmDecision(id: string): Promise<Decision>;
  rejectDecision(id: string): Promise<Decision>;
  /** Manual override: user-initiated order (e.g. close a position). */
  override(order: OrderRequest): Promise<ExecutionResult>;
  /** Live event stream (WS /ws). Returns an unsubscribe function. */
  subscribe(onEvent: (e: Event) => void): () => void;
}

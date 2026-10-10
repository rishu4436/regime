// Real client for Bot 6's FastAPI (backend/app). Paths and payloads follow backend/app/API.md exactly.
import type { Alert, AutoConfig, BacktestRequest, BacktestResult, Candle, ControlState, Decision, Event, LoopState, ManualStrategy,
  PerformanceSummary, Portfolio, RegimeAnalysis, StatusSnapshot, StrategySelection, SystemInfo, Timeframe, TokenInfo, TraderStyle } from "../contracts";
import { ApiError, type ActivateManualBody, type ApiClient, type ParseResult } from "./types";

const enc = encodeURIComponent;

export class HttpApi implements ApiClient {
  readonly kind = "http" as const;
  constructor(private base: string = "") {}

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    let res: Response;
    try {
      res = await fetch(this.base + path, {
        method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) { throw new ApiError(0, `backend unreachable (${e instanceof Error ? e.message : e})`); }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        // FastAPI: {"detail": "..."} or 422 {"detail": [{loc, msg}, ...]}
        detail = typeof j.detail === "string" ? j.detail
          : Array.isArray(j.detail) ? j.detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ")
          : JSON.stringify(j);
      } catch { /* not json */ }
      throw new ApiError(res.status, detail);
    }
    return res.json() as Promise<T>;
  }
  private get<T>(p: string) { return this.req<T>("GET", p); }
  private post<T>(p: string, body: unknown = {}) { return this.req<T>("POST", p, body); }

  health() { return this.get<{ status: string; contracts_version?: string; trading_mode?: string }>("/health"); }
  system() { return this.get<SystemInfo>("/api/system"); }
  getTokens(q = "") { return this.get<TokenInfo[]>(`/api/tokens?q=${enc(q)}`); }
  getCandles(symbol: string, timeframe: Timeframe = "1h", limit = 200) { return this.get<Candle[]>(`/api/candles/${enc(symbol)}?timeframe=${timeframe}&limit=${limit}`); }
  getPrice(symbol: string) { return this.get<{ symbol: string; price: number }>(`/api/price/${enc(symbol)}`); }
  getRegime(symbol: string, timeframe: Timeframe = "1h") { return this.get<RegimeAnalysis>(`/api/regime/${enc(symbol)}?timeframe=${timeframe}`); }
  getSelection(symbol: string, style: TraderStyle = "BALANCED", timeframe: Timeframe = "1h") {
    return this.get<StrategySelection>(`/api/auto/selection?symbol=${enc(symbol)}&style=${style}&timeframe=${timeframe}`);
  }
  listStrategies() { return this.get<ManualStrategy[]>("/api/strategies"); }
  saveStrategy(s: ManualStrategy) { return this.post<ManualStrategy>("/api/strategies", s); }
  getStrategy(id: string) { return this.get<ManualStrategy>(`/api/strategies/${enc(id)}`); }
  updateStrategy(id: string, s: ManualStrategy) { return this.req<ManualStrategy>("PUT", `/api/strategies/${enc(id)}`, s); }
  deleteStrategy(id: string) { return this.req<{ deleted: string }>("DELETE", `/api/strategies/${enc(id)}`); }
  validateStrategy(json: unknown) { return this.post<ManualStrategy>("/api/strategies/validate", json); }
  backtest(r: BacktestRequest) { return this.post<BacktestResult>("/api/backtest", r); }
  backtestCandles(symbol: string, timeframe: Timeframe = "1h", limit = 500) { return this.getCandles(symbol, timeframe, limit); }
  activateManual(body: ActivateManualBody) { return this.post<StatusSnapshot>("/api/manual/activate", body); }
  activateAuto(c: AutoConfig) { return this.post<StatusSnapshot>("/api/auto/activate", c); }
  deactivate() { return this.post<StatusSnapshot>("/api/agent/deactivate"); }
  getLoop() { return this.get<LoopState>("/api/loop"); }
  startLoop(interval_seconds?: number) { return this.post<LoopState>("/api/loop/start", interval_seconds ? { interval_seconds } : {}); }
  stopLoop() { return this.post<LoopState>("/api/loop/stop"); }
  tick() { return this.post<Decision[]>("/api/loop/tick"); }
  getStatus() { return this.get<StatusSnapshot>("/api/status"); }
  getPortfolio() { return this.get<Portfolio>("/api/portfolio"); }
  getDecisions(limit = 50, symbol?: string) { return this.get<Decision[]>(`/api/decisions?limit=${limit}${symbol ? `&symbol=${enc(symbol)}` : ""}`); }
  getDecision(id: string) { return this.get<Decision>(`/api/decisions/${enc(id)}`); }
  getConfirmations() { return this.get<Decision[]>("/api/confirmations"); }
  approve(id: string) { return this.post<Decision>(`/api/confirmations/${enc(id)}/approve`); }
  reject(id: string, reason?: string) { return this.post<Decision>(`/api/confirmations/${enc(id)}/reject`, reason ? { reason } : {}); }
  closePosition(symbol: string) { return this.post<Decision>(`/api/positions/${enc(symbol)}/close`); }
  getControl() { return this.get<ControlState>("/api/control"); }
  pause(reason?: string) { return this.post<ControlState>("/api/control/pause", reason ? { reason } : {}); }
  resume() { return this.post<ControlState>("/api/control/resume"); }
  resetEmergencyStop() { return this.req<ControlState>("POST", "/api/control/reset-emergency-stop"); }
  emergencyStop(reason?: string, flatten = false) { return this.post<ControlState>("/api/control/emergency-stop", { reason, flatten }); }
  setConfirmationMode(enabled: boolean) { return this.post<ControlState>("/api/control/confirmation-mode", { enabled }); }
  getAlerts(limit = 50) { return this.get<Alert[]>(`/api/alerts?limit=${limit}`); }
  getPerformance() { return this.get<PerformanceSummary>("/api/performance"); }
  llmParse(text: string, default_symbol?: string) { return this.post<ParseResult>("/api/llm/parse", { text, default_symbol }); }
  llmAsk(question: string) { return this.post<{ answer: string }>("/api/llm/ask", { question }); }
  llmSummary() { return this.get<{ summary: string }>("/api/llm/summary"); }

  subscribe(onEvent: (e: Event) => void): () => void {
    let ws: WebSocket | null = null, closed = false, timer: number | undefined, ping: number | undefined;
    const u = new URL((this.base || window.location.origin) + "/ws", window.location.href);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    const connect = () => {
      ws = new WebSocket(u.toString());
      ws.onopen = () => { ping = window.setInterval(() => ws?.readyState === 1 && ws.send(JSON.stringify({ type: "ping" })), 25_000); };
      ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* ignore malformed */ } };
      ws.onclose = () => { window.clearInterval(ping); if (!closed) timer = window.setTimeout(connect, 3000); };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); window.clearInterval(ping); ws?.close(); };
  }
}

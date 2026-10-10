// Real client for Bot 6's FastAPI (backend/app). Paths follow backend/app/README.md "Suggested API";
// adjust here if backend/app/API.md differs.
import type { AutoConfig, BacktestResult, Candle, ControlState, Decision, Event, ExecutionResult, ManualStrategy,
  OrderRequest, StatusSnapshot, Timeframe, TokenInfo } from "../contracts";
import type { ApiClient, BacktestRequest } from "./types";

export class HttpApi implements ApiClient {
  readonly kind = "http" as const;
  constructor(private base: string = "") {}

  private async req<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
    const { json, ...rest } = init ?? {};
    const res = await fetch(this.base + path, {
      ...rest,
      method: rest.method ?? (json !== undefined ? "POST" : "GET"),
      headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text().catch(() => "")}`);
    return res.json() as Promise<T>;
  }
  private post<T>(path: string, json: unknown = {}) { return this.req<T>(path, { json }); }

  health() { return this.req<{ status: string; trading_mode?: string }>("/health"); }
  getStatus() { return this.req<StatusSnapshot>("/api/status"); }
  getTokens(q = "") { return this.req<TokenInfo[]>(`/api/tokens?q=${encodeURIComponent(q)}`); }
  getCandles(symbol: string, timeframe: Timeframe = "1h", limit = 250) {
    return this.req<Candle[]>(`/api/candles/${encodeURIComponent(symbol)}?timeframe=${timeframe}&limit=${limit}`);
  }
  activateManual(s: ManualStrategy) { return this.post<StatusSnapshot>("/api/strategy/manual", s); }
  activateAuto(c: AutoConfig) { return this.post<StatusSnapshot>("/api/strategy/auto", c); }
  backtest(r: BacktestRequest) { return this.post<BacktestResult>("/api/backtest", r); }
  pause(reason = "") { return this.post<ControlState>("/api/control/pause", { reason }); }
  resume() { return this.post<ControlState>("/api/control/resume"); }
  emergencyStop(reason = "") { return this.post<ControlState>("/api/control/emergency-stop", { reason }); }
  setConfirmationMode(enabled: boolean) { return this.post<ControlState>("/api/control/confirmation", { enabled }); }
  confirmDecision(id: string) { return this.post<Decision>(`/api/decisions/${id}/confirm`); }
  rejectDecision(id: string) { return this.post<Decision>(`/api/decisions/${id}/reject`); }
  override(o: OrderRequest) { return this.post<ExecutionResult>("/api/override", o); }

  subscribe(onEvent: (e: Event) => void): () => void {
    let ws: WebSocket | null = null, closed = false, timer: number | undefined;
    const url = (() => {
      const u = new URL((this.base || window.location.origin) + "/ws", window.location.href);
      u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
      return u.toString();
    })();
    const connect = () => {
      ws = new WebSocket(url);
      ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* ignore malformed */ } };
      ws.onclose = () => { if (!closed) timer = window.setTimeout(connect, 3000); };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); ws?.close(); };
  }
}

// LIVE / 24-7 monitor state shared by the shell and views: /api/system, the live gate, /api/wallet and the loop heartbeat.
// Endpoints a backend doesn't have yet resolve to null; the UI derives what it can and says "not available".
import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { StatusSnapshot, SystemInfo } from "../contracts";
import { api } from "../api";
import type { AllowedTokens, LiveGate, LiveLimits, LoopHeartbeat, LoopState, WalletBalance, WalletInfo } from "../api/types";

export interface Heartbeat { runningSince: number | null; lastTick: number | null; nextTick: number | null; stale: boolean; observed: boolean }
export interface LiveState {
  system: SystemInfo | null;
  gate: LiveGate | null;
  gateSource: "backend" | "derived" | null;
  wallet: WalletInfo | null;
  walletEndpoint: boolean | null;     // false = GET /api/wallet not available on this backend
  loop: (LoopState & LoopHeartbeat) | null;
  hb: Heartbeat;
  now: number;
  isLive: boolean;
  refresh: () => void;
}
const noHb: Heartbeat = { runningSince: null, lastTick: null, nextTick: null, stale: false, observed: true };
export const LiveContext = createContext<LiveState>({ system: null, gate: null, gateSource: null, wallet: null, walletEndpoint: null, loop: null, hb: noHb, now: 0, isLive: false, refresh: () => {} });
export const useLive = () => useContext(LiveContext);

export const bscTx = (hash: string) => `https://bscscan.com/tx/${hash}`;
export const bscAddr = (addr: string) => `https://bscscan.com/address/${addr}`;
export const shortHex = (h: string, n = 6) => (h.length > 2 * n + 2 ? `${h.slice(0, n + 2)}…${h.slice(-n)}` : h);
export const balAsset = (b: WalletBalance) => b.asset ?? b.symbol ?? "?";
export const balAmount = (b: WalletBalance) => b.amount ?? b.quantity ?? 0;
export const balValue = (b: WalletBalance) => b.usd_value ?? b.value_usdt ?? null;
/** Stricter of our cap and the wallet's own (Agentic Wallet) limit, when it exposes one. */
export const stricter = (ours?: number | null, wallet?: number | null) =>
  ours == null ? wallet ?? null : wallet == null ? ours : Math.min(ours, wallet);
/** allowed_tokens may be an array, "ALL", "LIMITED" (baw wallet), null or undefined. */
export const allowedTokensText = (a: AllowedTokens): string | null =>
  Array.isArray(a) ? (a.length ? a.join(", ") : null) : typeof a === "string" ? (a.toUpperCase() === "ALL" ? "All tokens" : a.toUpperCase() === "LIMITED" ? "Limited by wallet" : a) : null;
export const limitsOf = (w: WalletInfo | null): LiveLimits => w?.limits ?? {};

/** Without a live-gate endpoint: LIVE only starts with LIVE_TRADING_ENABLED and BINANCE_LIVE_ENABLED (API.md v0.6), so
 * those are implied in LIVE; allow_live / missing switches come from /api/system if the backend adds them. */
export function deriveGate(system: SystemInfo | null, status: StatusSnapshot | null, wallet: WalletInfo | null): LiveGate | null {
  if (!system) return null;
  const sx = system as SystemInfo & { allow_live?: boolean | null; live_missing?: string[] };
  const live = system.trading_mode === "LIVE";
  const missing = sx.live_missing ?? [];
  const has = (k: string) => (missing.some((m) => m.includes(k)) ? false : live ? true : null);
  const reasons: string[] = [];
  const c = status?.control;
  if (c?.emergency_stopped) reasons.push(`Emergency stop${c.reason ? `: ${c.reason}` : ""}`);
  else if (c?.paused) reasons.push(`Paused${c.reason ? `: ${c.reason}` : ""}`);
  for (const m of missing) reasons.push(`Missing: ${m}`);
  return { live_trading_enabled: has("LIVE_TRADING_ENABLED"), binance_live_enabled: has("BINANCE_LIVE_ENABLED"),
    risk_allow_live: sx.allow_live ?? null, wallet_connected: wallet?.connected ?? null, can_trade: reasons.length === 0, blocked_reasons: reasons };
}

export function useLiveState(status: StatusSnapshot | null, tick: number): LiveState {
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [gateRaw, setGateRaw] = useState<LiveGate | null>(null);
  const [wallet, setWallet] = useState<WalletInfo | null>(null);
  const [walletEndpoint, setWalletEndpoint] = useState<boolean | null>(null);
  const [loop, setLoop] = useState<(LoopState & LoopHeartbeat) | null>(null);
  const [now, setNow] = useState(Date.now());
  const seen = useRef<{ ticks: number; at: number | null; since: number | null }>({ ticks: -1, at: null, since: null });
  const lastFull = useRef(0);

  const loadLoop = () => api.getLoop().then((l) => {
    const L = l as LoopState & LoopHeartbeat;
    const s = seen.current;
    if (L.ticks !== s.ticks) { if (s.ticks >= 0) s.at = Date.now(); s.ticks = L.ticks; }
    if (L.running && s.since == null) s.since = Date.now();
    if (!L.running) s.since = null;
    setLoop(L);
  }).catch(() => {});
  const load = () => {
    lastFull.current = Date.now();
    api.system().then(setSystem).catch(() => {});
    api.getLiveGate().then(setGateRaw).catch(() => setGateRaw(null));
    api.getWallet().then((w) => { setWallet(w); setWalletEndpoint(w != null || api.kind === "mock"); }).catch(() => setWalletEndpoint(null));
    loadLoop();
  };
  useEffect(load, []);
  // balances / caps move after fills: refresh with the loop at most every 4 s, and on control changes
  useEffect(() => { if (Date.now() - lastFull.current > 4000) load(); else loadLoop(); }, [tick, status?.control.updated_at]);
  // heartbeat clock: keeps "last tick 12 s ago" and the stale warning honest even when no events arrive
  useEffect(() => { const t = window.setInterval(() => { setNow(Date.now()); loadLoop(); }, 5000); return () => window.clearInterval(t); }, []);

  const iv = (loop?.interval_seconds ?? 5) * 1000;
  const H = loop?.heartbeat;
  const lastTick = H?.last_tick_at ? Date.parse(H.last_tick_at) : loop?.last_tick_at ? Date.parse(loop.last_tick_at) : seen.current.at;
  const runningSince = H?.started_at ? Date.parse(H.started_at) : loop?.started_at ? Date.parse(loop.started_at) : seen.current.since;
  const nextTick = loop?.next_tick_at ? Date.parse(loop.next_tick_at) : loop?.running && lastTick ? lastTick + iv : null;
  const stale = !!loop?.running && lastTick != null && now - lastTick > Math.max(3 * iv, 15000);
  const hb: Heartbeat = { runningSince, lastTick, nextTick, stale, observed: !H?.started_at && !loop?.started_at };
  const gate = gateRaw ?? deriveGate(system, status, wallet);
  const isLive = system?.trading_mode === "LIVE" || status?.control.trading_mode === "LIVE";
  return { system, gate, gateSource: gateRaw ? "backend" : gate ? "derived" : null, wallet, walletEndpoint, loop, hb, now, isLive, refresh: load };
}

export const ago = (t: number | null, now: number) => {
  if (t == null) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s ago` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
};
export const dur = (t: number | null, now: number) => {
  if (t == null) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000)), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
};

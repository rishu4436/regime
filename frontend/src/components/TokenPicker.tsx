// Tradable-token picker: GET /api/tokens (with Bot 4's token_status) merged with the built-in list, so the user picks
// which token the strategy trades and sees, per token, whether it's trading now and its live minimum order.
import { useEffect, useState } from "react";
import { api } from "../api";
import type { TokenInfo } from "../contracts";
import { TOKENS, isXStock } from "../lib/strategy";
import { useLive } from "../lib/live";

type TokenStatus = { trading_status?: string | null; tradable?: boolean | null; min_order_usdt?: number | null; next_open_time?: string | null };
export type TokenRow = { symbol: string; label: string; status: TokenStatus | null; price?: number | null; fromBackend: boolean;
  name?: string | null; type?: string | null; volume_24h?: number | null; liquidity?: number | null; narrative?: string | null };
/** Bot 4's full token rows put status fields at the top level (status, tradable, min_order_usdt); older rows nest token_status. */
type BackendToken = TokenInfo & { token_status?: TokenStatus | null; status?: string | null; tradable?: boolean | null; min_order_usdt?: number | null;
  type?: string | null; liquidity?: number | null; narrative?: string | null; next_open_time?: string | null };
const statusOf = (t: BackendToken): TokenStatus | null => t.token_status ?? (t.status != null || t.tradable != null || t.min_order_usdt != null
  ? { trading_status: t.status ? String(t.status).toUpperCase() : null, tradable: t.tradable ?? null, min_order_usdt: t.min_order_usdt ?? null, next_open_time: t.next_open_time ?? null } : null);

const STATUS_TEXT: Record<string, string> = { TRADING: "trading now", OFFHOURS: "market closed", PAUSED: "paused", HALTED: "halted", NO_LIQUIDITY: "no liquidity on BSC", DELISTED: "delisted" };
const LIVE_MIN_ORDER = 10;

let cache: { at: number; rows: TokenRow[] } | null = null;
export function useTokenRows(): TokenRow[] {
  const fallback = TOKENS.map((t) => ({ symbol: t.symbol, label: t.label, status: null, fromBackend: false }));
  const [rows, setRows] = useState<TokenRow[]>(cache?.rows ?? fallback);
  useEffect(() => {
    if (cache && Date.now() - cache.at < 60_000) return;
    let off = false;
    api.getTokens().then((list) => {
      if (off || !list?.length) return;
      const seen = new Set<string>();
      const merged: TokenRow[] = (list as BackendToken[]).map((t) => {
        seen.add(t.symbol);
        const known = TOKENS.find((k) => k.symbol === t.symbol);
        return { symbol: t.symbol, label: known?.label ?? `${t.symbol}${t.name ? ` (${t.name})` : ""}`, status: statusOf(t), price: t.price ?? null, fromBackend: true,
          name: t.name ?? null, type: t.type ?? t.token_type ?? null, volume_24h: t.volume_24h ?? null, liquidity: t.liquidity ?? null, narrative: t.narrative ?? null };
      });
      // full list is ~680 rows: tradable first, then by DEX volume, closed/not-tradable last
      const rank = (r: TokenRow) => (blocked(r) ? 2 : r.status?.tradable === false || (r.status?.trading_status && r.status.trading_status !== "TRADING") ? 1 : 0);
      const all: TokenRow[] = [...merged, ...(fallback as TokenRow[]).filter((f) => !seen.has(f.symbol))]
        .sort((a, b) => rank(a) - rank(b) || (b.volume_24h ?? 0) - (a.volume_24h ?? 0));
      cache = { at: Date.now(), rows: all }; setRows(all);
    }).catch(() => {});
    return () => { off = true; };
  }, []);
  return rows;
}

/** Blocked for live buys: no liquidity / not tradable / xStock. Off-hours tokens stay selectable (exits + queued buys). */
export const blocked = (r: TokenRow) => r.status?.tradable === false || r.status?.trading_status === "NO_LIQUIDITY" || isXStock(r.symbol);
const minOrder = (r: TokenRow) => Math.max(LIVE_MIN_ORDER, r.status?.min_order_usdt ?? 0);
export const detail = (r: TokenRow, live: boolean) => {
  const parts: string[] = [];
  const st = r.status?.trading_status; if (st) parts.push(STATUS_TEXT[st] ?? st.toLowerCase().replace(/_/g, " "));
  else if (isXStock(r.symbol)) parts.push(STATUS_TEXT.NO_LIQUIDITY);
  if (live && !blocked(r)) parts.push(`min $${minOrder(r)}`);
  return parts.join(" · ");
};

/** Drop-in <select> for a strategy's token, with live status and minimum order per option, plus a one-line summary. */
export function TokenPicker({ value, onChange, simOnly }: { value: string; onChange: (s: string) => void; simOnly: boolean }) {
  const rows = useTokenRows();
  const { isLive, wallet } = useLive();
  const sel = rows.find((r) => r.symbol === value);
  const usdt = wallet?.balances?.find((b) => (b.asset ?? b.symbol) === "USDT");
  const cash = usdt ? (usdt.amount ?? usdt.quantity ?? null) : null;
  return (
    <>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {!sel && <option value={value}>{value}</option>}
        {rows.map((r) => { const d = detail(r, isLive), off = blocked(r) && !simOnly;
          return <option key={r.symbol} value={r.symbol} disabled={off} title={off ? "Can't be bought live right now" : undefined}>{r.label}{d ? ` · ${d}` : ""}</option>; })}
      </select>
      {sel && (sel.status || isLive) && <div className="small faint" style={{ marginTop: 4 }}>
        {detail(sel, isLive) || "status not reported"}
        {sel.status?.trading_status === "OFFHOURS" && sel.status.next_open_time ? ` · opens ${new Date(sel.status.next_open_time).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : ""}
        {isLive && cash != null && !blocked(sel) && cash < minOrder(sel) ? <span className="neg"> · wallet USDT ({cash.toFixed(2)}) is below this token's minimum order</span> : null}
      </div>}
    </>
  );
}

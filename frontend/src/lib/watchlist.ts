// Watchlist + Auto-pick helpers: narrative labels, the local sector map (fallback when the backend sends no narrative),
// and how watchlist fields are added to activation bodies without breaking backends that don't know them yet.
import type { AutoPickConfig, Narrative, WatchlistFields } from "../api/types";
import { ApiError } from "../api/types";

export const NARRATIVES: { value: Narrative; label: string }[] = [
  { value: "AI_CHIPS", label: "AI & chips" }, { value: "EV_ENERGY", label: "EV & energy" }, { value: "BIG_TECH", label: "Big Tech" },
  { value: "CRYPTO", label: "Crypto-linked" }, { value: "ANY", label: "Any" },
];
export const MAX_WATCH = 5;
export const DEFAULT_AUTO_PICK: AutoPickConfig = { enabled: false, max_tokens: 3, narrative: "ANY" };

const SECTOR: Record<string, Narrative> = {
  NVDA: "AI_CHIPS", AMD: "AI_CHIPS", AVGO: "AI_CHIPS", TSM: "AI_CHIPS", MU: "AI_CHIPS", ARM: "AI_CHIPS", INTC: "AI_CHIPS", QCOM: "AI_CHIPS", PLTR: "AI_CHIPS",
  TSLA: "EV_ENERGY", RIVN: "EV_ENERGY", ENPH: "EV_ENERGY", NIO: "EV_ENERGY", LCID: "EV_ENERGY",
  AAPL: "BIG_TECH", MSFT: "BIG_TECH", GOOGL: "BIG_TECH", GOOG: "BIG_TECH", AMZN: "BIG_TECH", META: "BIG_TECH", NFLX: "BIG_TECH",
  COIN: "CRYPTO", MSTR: "CRYPTO", HOOD: "CRYPTO", MARA: "CRYPTO", RIOT: "CRYPTO", CRCL: "CRYPTO",
};
/** NVDAB / NVDAon / NVDAx -> NVDA */
export const underlyingOf = (sym: string) => sym.replace(/(on|x|B)$/, "");
/** Narrative code -> plain label. Accepts codes or labels from the backend ("AI_CHIPS", "AI & chips"). */
export const narrativeLabel = (n?: string | null) => {
  if (!n) return null;
  const hit = NARRATIVES.find((x) => x.value === n.toUpperCase() || x.label.toLowerCase() === n.toLowerCase());
  return hit ? (hit.value === "ANY" ? null : hit.label) : n.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
};
export const narrativeOf = (sym: string): Narrative | null => SECTOR[underlyingOf(sym)] ?? null;
export const narrativeText = (sym: string, fromBackend?: string | null) => narrativeLabel(fromBackend) ?? narrativeLabel(narrativeOf(sym));

/** The watchlist the user set, cleaned: unique, max 5, never empty (falls back to the strategy's symbol). */
export function cleanWatchlist(w: string[] | undefined | null, symbol: string): string[] {
  const out = [...new Set((w ?? []).filter(Boolean))].slice(0, MAX_WATCH);
  return out.length ? out : [symbol];
}
/** True when the user uses more than the single-token behaviour (only then are extra fields sent). */
export const usesWatchlist = (f: WatchlistFields, symbol: string) => !!f.auto_pick?.enabled || cleanWatchlist(f.watchlist, symbol).length > 1;
/** Extra fields to merge into an activation body; {} for the plain single-token case so older backends see exactly today's JSON. */
export function watchlistExtras(f: WatchlistFields, symbol: string): WatchlistFields {
  if (!usesWatchlist(f, symbol)) return {};
  return { watchlist: cleanWatchlist(f.watchlist, symbol), auto_pick: { ...DEFAULT_AUTO_PICK, ...f.auto_pick } };
}
/** A backend that doesn't know the watchlist fields yet rejects them (contract models forbid extra fields). */
export const isUnknownWatchlistField = (e: unknown) =>
  e instanceof ApiError && e.status === 422 && /watchlist|auto_pick|extra/i.test(e.detail);

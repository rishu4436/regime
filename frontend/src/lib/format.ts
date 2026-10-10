export const usd = (v: number | null | undefined, d = 2) =>
  v == null || !isFinite(v) ? "—" : `$${v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
export const pct = (v: number | null | undefined, d = 2, sign = true) =>
  v == null || !isFinite(v) ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(d)}%`;
export const num = (v: number | null | undefined, d = 4) => (v == null || !isFinite(v) ? "—" : v.toFixed(d));
export const cls = (v: number | null | undefined) => (v == null ? "" : v > 0 ? "positive" : v < 0 ? "negative" : "");
/** Times shown in the browser's local zone. */
export const time = (iso?: string | null) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
export const humanize = (s?: string | null) => (s ? s.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()) : "—");

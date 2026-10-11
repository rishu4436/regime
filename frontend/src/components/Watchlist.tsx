// Watchlist picker (1-5 tokens) with Auto-pick by narrative, replacing the single-token picker in Auto Mode and the
// Manual Builder, plus the dashboard Watchlist card. A plain one-token watchlist sends exactly today's JSON.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { AutoPickConfig, AutoPickResult, Narrative, WatchlistEntry, WatchlistFields, WatchlistInfo } from "../api/types";
import type { Decision, StatusSnapshot } from "../contracts";
import { blocked, detail, useTokenRows, type TokenRow } from "./TokenPicker";
import { Card, NumberInput, Toggle } from "./ui";
import { RegimeBadge } from "./Badges";
import { useLive } from "../lib/live";
import { cleanWatchlist, DEFAULT_AUTO_PICK, MAX_WATCH, NARRATIVES, narrativeText } from "../lib/watchlist";
import { humanReason } from "../lib/strategy";
import { usd } from "../lib/format";

export interface WatchValue extends WatchlistFields { symbol: string }

/** Auto-pick preview: GET /api/autopick (null on backends without it). */
function useAutoPick(ap: AutoPickConfig | undefined) {
  const [res, setRes] = useState<AutoPickResult | null | undefined>(undefined);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!ap?.enabled) { setRes(undefined); return; }
    let off = false; setErr(null);
    const t = window.setTimeout(() => api.getAutoPick(ap.narrative, ap.max_tokens)
      .then((r) => !off && setRes(r)).catch((e) => { if (!off) { setRes(null); setErr(e instanceof Error ? e.message : String(e)); } }), 250);
    return () => { off = true; window.clearTimeout(t); };
  }, [ap?.enabled, ap?.narrative, ap?.max_tokens]);
  return { res, err };
}

export function WatchlistPicker({ value, onChange, simOnly }: { value: WatchValue; onChange: (v: WatchValue) => void; simOnly: boolean }) {
  const rows = useTokenRows();
  const { isLive } = useLive();
  const ap: AutoPickConfig = { ...DEFAULT_AUTO_PICK, ...value.auto_pick };
  const list = cleanWatchlist(value.watchlist, value.symbol);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const { res, err } = useAutoPick(value.auto_pick?.enabled ? ap : undefined);

  const setList = (next: string[]) => onChange({ ...value, watchlist: next, symbol: next[0] ?? value.symbol });
  const setAp = (patch: Partial<AutoPickConfig>) => onChange({ ...value, watchlist: list, auto_pick: { ...ap, ...patch } });
  // with Auto-pick on, the strategy's main symbol follows the top pick (back-compat field for single-token backends)
  const top = res?.picks?.[0]?.symbol;
  useEffect(() => { if (ap.enabled && top && top !== value.symbol) onChange({ ...value, symbol: top }); }, [ap.enabled, top]);

  const rowOf = (sym: string): TokenRow | undefined => rows.find((r) => r.symbol === sym);
  const ql = q.trim().toLowerCase();
  const matches = useMemo(() => rows.filter((r) => !list.includes(r.symbol) && (!ql || r.symbol.toLowerCase().includes(ql) || (r.label + " " + (r.name ?? "")).toLowerCase().includes(ql)
    || (narrativeText(r.symbol, r.narrative) ?? "").toLowerCase().includes(ql))), [rows, list, ql]);
  const full = list.length >= MAX_WATCH;

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="rule on" style={{ margin: 0 }}>
        <Toggle checked={ap.enabled} onChange={(v) => setAp({ enabled: v })} label="Auto-pick" />
        <div className="rule-text"><div className="rule-title">Auto-pick tokens</div>
          <div className="rule-hint">Let the agent choose 1–5 tokens for a theme and trade among them.</div></div>
      </div>

      {ap.enabled ? (
        <>
          <div className="field-grid">
            <div><span className="lbl">How many tokens</span>
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <input type="range" min={1} max={MAX_WATCH} step={1} value={ap.max_tokens} onChange={(e) => setAp({ max_tokens: +e.target.value })} style={{ flex: 1 }} aria-label="How many tokens" />
                <NumberInput value={ap.max_tokens} min={1} max={MAX_WATCH} width={70} onChange={(v) => setAp({ max_tokens: Math.max(1, Math.min(MAX_WATCH, v ?? 1)) })} />
              </div></div>
            <div><span className="lbl">Theme</span>
              <select value={ap.narrative} onChange={(e) => setAp({ narrative: e.target.value as Narrative })}>
                {NARRATIVES.map((n) => <option key={n.value} value={n.value}>{n.label}</option>)}
              </select></div>
          </div>
          <div>
            <span className="lbl">Agent's picks right now {(res?.example || api.kind === "mock") && <span className="pill warn" style={{ marginLeft: 6, fontSize: ".68rem" }}>example data</span>}</span>
            {res === undefined ? <div className="small faint">Ranking tokens…</div>
              : res === null ? <div className="small faint">{err ? `Couldn't load picks: ${err}` : "Preview not available on this agent yet. It picks when it starts."}</div>
              : !res.picks?.length ? <div className="small faint">No token fits this theme right now. Try "Any" or another theme.</div>
              : <div className="stack" style={{ gap: 6 }}>
                  {res.picks.map((p, i) => (
                    <div key={p.symbol} className="small" style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                      <span className="faint">{i + 1}.</span><b>{p.symbol}</b>
                      {narrativeText(p.symbol, p.narrative) && <span className="pill" style={{ fontSize: ".66rem" }}>{narrativeText(p.symbol, p.narrative)}</span>}
                      <span className="muted grow">{humanReason(p.reason)}</span>
                      {p.score != null && <span className="faint" title="Ranking score out of 100">{Math.round(p.score)}</span>}
                    </div>))}
                </div>}
            {!!res?.skipped?.length && <details className="small faint" style={{ marginTop: 6 }}><summary>Skipped {res.skipped.length}</summary>
              {res.skipped.map((s) => <div key={s.symbol}>{s.symbol}: {humanReason(s.reason) || "not picked"}</div>)}</details>}
          </div>
        </>
      ) : (
        <div>
          <span className="lbl">Watchlist <span className="faint">({list.length}/{MAX_WATCH})</span></span>
          <div className="chips">
            {list.map((sym, i) => { const r = rowOf(sym), n = narrativeText(sym, r?.narrative);
              return (
                <span key={sym} className="chip pass" title={r ? `${r.label}${detail(r, isLive) ? ` · ${detail(r, isLive)}` : ""}` : sym}>
                  <b>{sym}</b>{n && <span className="faint">· {n}</span>}{i === 0 && list.length > 1 && <span className="faint">· main</span>}
                  {list.length > 1 && <button type="button" aria-label={`Remove ${sym}`} onClick={() => setList(list.filter((x) => x !== sym))}
                    style={{ background: "none", border: 0, color: "inherit", cursor: "pointer", padding: 0, marginLeft: 2 }}>✕</button>}
                </span>); })}
          </div>
          <div style={{ position: "relative", marginTop: 8 }}>
            <input type="search" placeholder={full ? "Watchlist is full (5 tokens)" : "Search tokens: NVDA, Tesla, AI…"} value={q} disabled={full}
              onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => window.setTimeout(() => setOpen(false), 150)}
              style={{ width: "100%" }} />
            {open && !full && (
              <div className="card" style={{ position: "absolute", zIndex: 20, left: 0, right: 0, top: "calc(100% + 4px)", maxHeight: 260, overflowY: "auto", padding: 6 }}>
                {matches.slice(0, 30).map((r) => { const off = blocked(r) && !simOnly, n = narrativeText(r.symbol, r.narrative), d = detail(r, isLive);
                  return (
                    <button key={r.symbol} type="button" disabled={off} title={off ? "Can't be bought live right now" : undefined}
                      onMouseDown={(e) => e.preventDefault()} onClick={() => { setList([...list, r.symbol]); setQ(""); }}
                      style={{ display: "flex", width: "100%", gap: 8, alignItems: "baseline", textAlign: "left", background: "none", border: 0, color: "var(--text)", padding: "6px 8px", borderRadius: 8, cursor: off ? "not-allowed" : "pointer", opacity: off ? 0.45 : 1 }}>
                      <b>{r.symbol}</b><span className="small muted grow trunc">{r.name ?? r.label}</span>
                      {n && <span className="pill" style={{ fontSize: ".66rem" }}>{n}</span>}
                      {d && <span className="small faint">{d}</span>}
                    </button>); })}
                {!matches.length && <div className="small faint" style={{ padding: 8 }}>No token matches "{q}"</div>}
              </div>)}
          </div>
          <div className="small faint" style={{ marginTop: 4 }}>
            {list.length > 1 ? "The agent checks every token each bar and buys whichever one has a signal, within your trading limits." : "Add up to 4 more tokens to let the agent trade among them."}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- dashboard card
const entrySym = (e: string | WatchlistEntry) => (typeof e === "string" ? e : e.symbol);
const ACTION_TEXT: Record<string, string> = { BUY: "Buy", SELL: "Sell", STAY_OUT: "Stay out", HOLD: "Hold" };

/** Watchlist card: GET /api/watchlist, else status.watchlist / status.picks. Renders nothing for a single-token agent. */
export function WatchlistCard({ status, tick }: { status: StatusSnapshot | null; tick: number }) {
  const [info, setInfo] = useState<WatchlistInfo | null>(null);
  useEffect(() => { if (status?.agent_mode) api.getWatchlist().then(setInfo).catch(() => setInfo(null)); else setInfo(null); }, [status?.agent_mode, tick]);
  const sx = (status ?? {}) as StatusSnapshot & Partial<WatchlistInfo>;
  const watch = (info?.watchlist?.length ? info.watchlist : sx.watchlist) ?? [];
  const picks = info?.picks ?? sx.picks ?? [];
  if (!status?.agent_mode || watch.length < 2) return null;
  const positions = status.portfolio?.positions ?? [];
  const lastFor = (sym: string): Decision | undefined => (status.recent_decisions ?? []).find((d) => d.symbol === sym);
  return (
    <Card pad={false} title={<>Watchlist <span className="muted small" style={{ fontWeight: 400 }}>{watch.length} tokens</span></>}
      extra={info?.auto_pick?.enabled && <span className="pill ok">Auto-pick · {NARRATIVES.find((n) => n.value === info.auto_pick!.narrative)?.label ?? "Any"}</span>}>
      <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
        <table>
          <thead><tr><th>Token</th><th>Regime</th><th>Last signal</th><th className="r">Position</th><th>Why it's here</th></tr></thead>
          <tbody>
            {watch.map((e) => {
              const sym = entrySym(e), o = typeof e === "string" ? null : e, d = lastFor(sym), p = picks.find((x) => x.symbol === sym);
              const held = positions.filter((x) => x.symbol === sym), val = held.reduce((a, x) => a + x.quantity * (x.current_price ?? x.avg_entry_price), 0);
              const act = o?.action ?? o?.signal ?? d?.action ?? null, regime = o?.regime ?? d?.regime ?? (sym === status.active_symbol ? status.current_regime : null);
              const n = narrativeText(sym, o?.narrative ?? p?.narrative);
              return (
                <tr key={sym}>
                  <td><b>{sym}</b>{n && <div className="faint small">{n}</div>}</td>
                  <td>{regime ? <RegimeBadge regime={regime as Decision["regime"]} /> : <span className="faint">—</span>}</td>
                  <td>{act ? <span className={`pill ${act}`}>{ACTION_TEXT[act] ?? act}</span> : <span className="faint">waiting</span>}</td>
                  <td className="r">{held.length ? usd(val) : o?.position_usdt ? usd(o.position_usdt) : <span className="faint">—</span>}</td>
                  <td className="wrap small muted">{humanReason(o?.reason ?? p?.reason) || (d ? humanReason(d.reason) : "")}</td>
                </tr>);
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

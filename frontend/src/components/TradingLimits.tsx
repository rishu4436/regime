// Trading limits panel: GET/PUT /api/settings/limits (Bot 6, localhost only). Lets a big wallet trade at a sensible size
// (e.g. 2% of 10,000 USDT = 200 per order) instead of the 25 USDT safe default. Hidden when the backend has no endpoint.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { ApiError, type TradingLimits, type TradingLimitsResponse } from "../api/types";
import { Card, NumberInput, Segmented, Skeleton, useToast } from "./ui";
import { useLive } from "../lib/live";
import { usd } from "../lib/format";

const FIELDS: { key: keyof Omit<TradingLimits, "order_mode" | "order_usdt" | "order_pct">; label: string; hint: string }[] = [
  { key: "max_order_usdt", label: "Max per order", hint: "No single buy can be bigger than this." },
  { key: "max_total_usdt", label: "Max total invested", hint: "All open positions together." },
  { key: "max_per_token_usdt", label: "Max in one token", hint: "Keeps one stock from taking over." },
  { key: "daily_loss_usdt", label: "Daily loss limit", hint: "Losing this much in a day triggers the emergency stop." },
  { key: "daily_buy_usdt", label: "Daily buy limit", hint: "The most Regime may buy in one day." },
];
// no order_mode / no size from the backend = the strategy decides the size
const modeOf = (l: Partial<TradingLimits>): TradingLimits["order_mode"] =>
  l.order_mode === "pct" || l.order_mode === "fixed" ? l.order_mode : "strategy";
const pick = (l: TradingLimitsResponse | TradingLimits): TradingLimits => ({
  order_mode: modeOf(l), order_usdt: l.order_usdt, order_pct: l.order_pct, max_order_usdt: l.max_order_usdt,
  max_total_usdt: l.max_total_usdt, max_per_token_usdt: l.max_per_token_usdt, daily_loss_usdt: l.daily_loss_usdt,
  ...(l.daily_buy_usdt != null ? { daily_buy_usdt: l.daily_buy_usdt } : {}),
});
/** Only what the user changed: risk keeps every other value as it is (e.g. the per-token cap). */
function changed(cur: TradingLimits, next: TradingLimits): Partial<TradingLimits> {
  const out: Partial<TradingLimits> = {};
  for (const k of Object.keys(next) as (keyof TradingLimits)[]) if (next[k] !== cur[k]) (out as Record<string, unknown>)[k] = next[k];
  if (out.order_mode === "strategy") { out.order_usdt = null as unknown as number; out.order_pct = null as unknown as number; }
  else if (out.order_mode === "fixed") out.order_usdt = next.order_usdt;
  else if (out.order_mode === "pct") out.order_pct = next.order_pct;
  return out;
}
const errText = (e: unknown) => e instanceof ApiError
  ? e.status === 403 ? "Only works on the PC running Regime. Open the dashboard there (localhost) to change limits."
    : e.status === 0 ? "Can't reach the agent right now. Try again in a moment." : e.detail
  : e instanceof Error ? e.message : String(e);

/** Which limits go up vs the saved ones (raising needs a confirmation). */
function raised(cur: TradingLimits, next: TradingLimits): string[] {
  const out: string[] = [];
  const perOrder = (l: TradingLimits) => (l.order_mode === "fixed" ? l.order_usdt : null);
  if (next.order_mode === cur.order_mode && next.order_mode === "pct" && next.order_pct > cur.order_pct) out.push(`order size ${cur.order_pct}% → ${next.order_pct}% of wallet`);
  else if (next.order_mode !== cur.order_mode && next.order_mode !== "strategy") out.push(next.order_mode === "pct" ? `order size → ${next.order_pct}% of wallet` : `order size → ${usd(next.order_usdt)}`);
  else if ((perOrder(next) ?? 0) > (perOrder(cur) ?? 0)) out.push(`order size ${usd(cur.order_usdt)} → ${usd(next.order_usdt)}`);
  for (const f of FIELDS) if ((next[f.key] ?? 0) > (cur[f.key] ?? Infinity)) out.push(`${f.label.toLowerCase()} ${usd(cur[f.key])} → ${usd(next[f.key])}`);
  return out;
}

export function TradingLimitsCard({ refresh }: { refresh?: () => void }) {
  const toast = useToast();
  const { wallet, refresh: refreshLive } = useLive();
  const [data, setData] = useState<TradingLimitsResponse | null | undefined>(undefined); // undefined = loading, null = no endpoint
  const [draft, setDraft] = useState<TradingLimits | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ body: TradingLimits; changes: string[]; reset?: boolean } | null>(null);

  const load = () => api.getLimits().then((d) => { setData(d); setDraft(d ? pick(d) : null); }).catch((e) => { setData((cur) => cur ?? null); setErr(errText(e)); });
  useEffect(() => { load(); }, []);

  const usdtBal = wallet?.balances?.find((b) => (b.asset ?? b.symbol) === "USDT");
  const walletUsdt = usdtBal ? usdtBal.amount ?? usdtBal.quantity ?? null : null;
  const walletCap = data?.wallet_daily_limit_usdt ?? null;
  const dirty = useMemo(() => !!(data && draft && JSON.stringify(pick(data)) !== JSON.stringify(draft)), [data, draft]);

  if (data === null) return null; // backend has no /api/settings/limits yet
  if (data === undefined || !draft) return <Card title="Trading limits"><Skeleton h={180} /></Card>;

  const set = <K extends keyof TradingLimits>(k: K, v: TradingLimits[K]) => { setDraft({ ...draft, [k]: v }); setErr(null); };
  const perOrder = draft.order_mode === "pct" ? (walletUsdt != null ? (walletUsdt * (draft.order_pct ?? 0)) / 100 : null) : draft.order_mode === "fixed" ? draft.order_usdt : null;
  const capped = perOrder != null ? Math.min(perOrder, draft.max_order_usdt) : null;
  const localIssue = draft.max_order_usdt > draft.max_total_usdt ? "Max per order can't be above max total invested."
    : draft.max_per_token_usdt > draft.max_total_usdt ? "Max in one token can't be above max total invested."
    : walletCap != null && [draft.max_order_usdt, draft.max_total_usdt, draft.max_per_token_usdt, draft.daily_loss_usdt].some((v) => v > walletCap) ? `No limit can go above the wallet's own daily limit (${usd(walletCap)}).`
    : null;

  const put = async (body: Partial<TradingLimits>, reset = false) => {
    setBusy(true); setErr(null);
    try {
      const d = await api.putLimits(body);
      setData(d); setDraft(pick(d)); setConfirm(null);
      toast(reset ? "Limits reset to safe defaults" : "Trading limits saved. The agent uses them from its next check", "success");
      refresh?.(); refreshLive();
    } catch (e) { setErr(errText(e)); setConfirm(null); } finally { setBusy(false); }
  };
  const save = () => {
    const changes = raised(pick(data), draft);
    if (changes.length) setConfirm({ body: draft, changes }); else put(changed(pick(data), draft));
  };
  const reset = () => data.defaults && setConfirm({ body: pick(data.defaults), changes: raised(pick(data), pick(data.defaults)), reset: true });
  const confirmBody = (c: { body: TradingLimits; reset?: boolean }) => (c.reset ? c.body : changed(pick(data), c.body));

  return (
    <Card title="Trading limits" extra={dirty && <span className="pill warn">Unsaved</span>}>
      <div className="stack" style={{ gap: 12 }}>
        {err && <div className="banner red" role="alert" style={{ marginBottom: 0 }}><span className="grow">{err}</span></div>}
        <div>
          <span className="lbl">Order size</span>
          <Segmented size="sm" value={draft.order_mode} onChange={(v) => { setDraft({ ...draft, order_mode: v, ...(v === "fixed" && draft.order_usdt == null ? { order_usdt: 25 } : {}), ...(v === "pct" && draft.order_pct == null ? { order_pct: 2 } : {}), ...(v === "strategy" ? { order_usdt: data.order_usdt, order_pct: data.order_pct } : {}) }); setErr(null); }} options={[{ value: "strategy", label: "Strategy decides" }, { value: "fixed", label: "Fixed USDT" }, { value: "pct", label: "% of wallet" }]} />
          <div style={{ marginTop: 8 }}>
            {draft.order_mode === "fixed"
              ? <NumberInput value={draft.order_usdt ?? 25} min={1} step={5} suffix="USDT" onChange={(v) => set("order_usdt", v ?? 1)} />
              : draft.order_mode === "pct" ? <NumberInput value={draft.order_pct ?? 2} min={0.1} max={100} step={0.5} suffix="%" onChange={(v) => set("order_pct", v ?? 0.1)} /> : null}
          </div>
          <div className="small faint" style={{ marginTop: 4 }}>
            {draft.order_mode === "strategy" ? "Each strategy sizes its own buys (DCA and Grid buy small); the limits below still trim them."
              : <>{draft.order_mode === "pct" ? "Grows and shrinks with your wallet." : "The same amount every buy."} This replaces the strategy's own size.</>}{" "}
            {walletUsdt != null && capped != null
              ? <>With your wallet of <b>{usd(walletUsdt)}</b> this is <b>{usd(capped)}</b> per order{perOrder != null && perOrder > draft.max_order_usdt ? " (held to the max per order)" : ""}.</>
              : draft.order_mode !== "pct" ? null : "Example: with 10,000 USDT, 2% is 200 USDT per order."}
          </div>
        </div>
        <div className="field-grid">
          {FIELDS.filter((f) => draft[f.key] != null).map((f) => (
            <div key={f.key}>
              <span className="lbl">{f.label}</span>
              <NumberInput value={draft[f.key] as number} min={1} step={5} suffix="USDT" onChange={(v) => set(f.key, v ?? 1)} />
              <div className="small faint" style={{ marginTop: 3 }}>{f.hint}</div>
            </div>
          ))}
        </div>
        {localIssue && <div className="err" style={{ marginTop: 0 }}>{localIssue}</div>}
        <div className="btn-row">
          <button className="btn" disabled={busy || !dirty || !!localIssue} onClick={save}>{busy ? "Saving…" : "Save limits"}</button>
          {dirty && <button className="btn ghost" disabled={busy} onClick={() => { setDraft(pick(data)); setErr(null); }}>Undo changes</button>}
          {data.defaults && <button className="btn ghost" disabled={busy} onClick={reset}>Reset to safe defaults</button>}
        </div>
        <div className="note" style={{ marginTop: 0 }}>
          Only changeable on the PC running Regime. The wallet's own daily limit in the Binance App stays the final cap{walletCap != null ? ` (${usd(walletCap)} today)` : ""}.
          Saving doesn't clear an emergency stop.
          {data.max_sell_usdt != null && <> Biggest single sell: <b>{usd(data.max_sell_usdt)}</b>, so a whole position can close in one order.</>}
        </div>
      </div>
      {confirm && (
        <div className="modal-bg" role="dialog" aria-modal="true" onClick={() => !busy && setConfirm(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{confirm.reset ? "Reset to safe defaults?" : "Raise your trading limits?"}</h3>
            <div style={{ fontSize: "0.88rem", color: "var(--muted)" }} className="stack">
              {confirm.changes.length > 0 && <div>You're raising: {confirm.changes.join("; ")}.</div>}
              <div className="kv">
                <span className="k">Worst-case daily loss</span><span><b>{usd(confirm.body.daily_loss_usdt)}</b> (then the emergency stop)</span>
                <span className="k">Most invested at once</span><span><b>{usd(confirm.body.max_total_usdt)}</b></span>
                <span className="k">Biggest single order</span><span><b>{usd(confirm.body.max_order_usdt)}</b></span>
              </div>
              <div className="small faint">The wallet's own daily limit in the Binance App stays the final cap.</div>
            </div>
            <div className="btn-row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
              <button className="btn ghost" onClick={() => setConfirm(null)} disabled={busy}>Cancel</button>
              <button className={`btn${confirm.reset ? "" : " red"}`} onClick={() => put(confirmBody(confirm), confirm.reset)} disabled={busy} autoFocus>{busy ? "Saving…" : confirm.reset ? "Reset limits" : "Confirm new limits"}</button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

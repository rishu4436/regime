// "Ask Stock Agent": a paid second opinion on one token via x402 (Bot 4's /api/x402, paid from the Agentic Wallet).
// Advice only: the report is shown here and never places or changes a trade.
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ApiError, type X402Price, type X402JobStatus } from "../api/types";
import { bscTx } from "../lib/live";
import { Card } from "./ui";

export function StockAgentCard({ symbol, tick }: { symbol: string; tick?: number }) {
  const [price, setPrice] = useState<X402Price | null | undefined>(undefined);
  const [job, setJob] = useState<{ id: string; paid?: number | null; tx?: string | null } | null>(null);
  const [res, setRes] = useState<X402JobStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // null = the backend has no /api/x402 yet (404/405/501): stop re-asking every tick until the symbol changes
  const missing = useRef<string | null>(null);
  useEffect(() => {
    if (missing.current === symbol) return;
    api.getX402Price(symbol).then((p) => { missing.current = p === null ? symbol : null; setPrice(p); }).catch(() => setPrice(null));
  }, [symbol, tick, job?.id]);
  useEffect(() => { setJob(null); setRes(null); setErr(null); }, [symbol]);
  // poll the job every 3 s until it's done or failed
  useEffect(() => {
    if (!job || (res && res.status !== "pending")) return;
    const id = setInterval(() => api.getX402Job(job.id).then(setRes).catch((e) => setRes({ status: "failed", error: e instanceof Error ? e.message : String(e) })), 3000);
    return () => clearInterval(id);
  }, [job, res]);

  if (!price?.enabled) return null;
  const cost = price.price_usdt ?? price.cap_usdt ?? 0.1, left = price.calls_left_today;
  const ask = async () => {
    setBusy(true); setErr(null); setRes(null);
    try { const j = await api.startX402Analysis(symbol); setJob({ id: j.job_id, paid: j.paid_usdt, tx: j.tx_hash }); setRes({ status: "pending" }); }
    catch (e) { setErr(e instanceof ApiError && e.status === 403 ? "Paid calls only work on the PC running the Regime backend." : e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return (
    <Card title="Second opinion" extra={<span className="pill">x402 · paid per call</span>}>
      <div className="small muted" style={{ marginBottom: 8 }}>Ask Binance Stock Agent for an analysis of <b>{symbol}</b>, paid from the Agentic Wallet. It's advice only and never places a trade.</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn" disabled={busy || res?.status === "pending" || left === 0} onClick={ask}>
          {busy ? <span className="spin" /> : `Ask Stock Agent (≈${cost.toFixed(2)} USDT)`}</button>
        <span className="small faint">{left != null ? `${left} call${left === 1 ? "" : "s"} left today` : ""}{price.quota_usdt_left != null ? ` · ${price.quota_usdt_left.toFixed(2)} USDT x402 quota left` : ""}</span>
      </div>
      {job && <div className="small faint" style={{ marginTop: 8 }}>Paid {job.paid != null ? `${job.paid.toFixed(2)} USDT` : ""}{job.tx ? <> · <a href={bscTx(job.tx)} target="_blank" rel="noreferrer">tx ↗</a></> : null}</div>}
      {res?.status === "pending" && <div className="small" style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center" }}><span className="spin" /> Waiting for the report…</div>}
      {res?.status === "done" && <div className="small" style={{ marginTop: 10, whiteSpace: "pre-wrap", lineHeight: 1.5, maxHeight: 320, overflow: "auto" }}>{res.report || "The agent returned an empty report."}</div>}
      {(err || res?.status === "failed") && <div className="banner red" style={{ marginTop: 10 }}><span className="grow small">{err ?? res?.error ?? "The analysis failed."}</span></div>}
    </Card>
  );
}

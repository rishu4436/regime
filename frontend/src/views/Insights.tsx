import { useEffect, useState } from "react";
import { api } from "../api";
import type { Alert } from "../api/types";
import { Card, Skeleton, useToast } from "../components/ui";
import { time } from "../lib/format";

const LEVEL: Record<Alert["level"], string> = { INFO: "", WARNING: "warn", CRITICAL: "bad" };

export function AlertsPanel({ version }: { version: number }) {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  useEffect(() => { api.getAlerts(20).then(setAlerts).catch(() => setAlerts([])); }, [version]);
  // the same alert repeated every tick (e.g. a risk block) shows once, with a count and the latest time
  const shown = (alerts ?? []).reduce<(Alert & { n: number })[]>((out, a) => {
    const prev = out.find((x) => x.code === a.code && x.message === a.message && x.symbol === a.symbol);
    if (prev) { prev.n += 1; if (String(a.timestamp) > String(prev.timestamp)) prev.timestamp = a.timestamp; } else out.push({ ...a, n: 1 });
    return out;
  }, []);
  return (
    <Card title={<>Alerts {alerts?.length ? <span className="muted small" style={{ fontWeight: 400 }}>{alerts.length}</span> : null}</>}>
      {!alerts ? <Skeleton h={60} /> : alerts.length ? (
        <div className="stack" style={{ gap: 8, maxHeight: 220, overflow: "auto" }}>
          {shown.map((a, i) => (
            <div key={a.id ?? i} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <span className={`pill ${LEVEL[a.level]}`} style={{ flex: "none" }}>{a.code.replace(/_/g, " ")}</span>
              <div className="small"><div>{a.message}{a.n > 1 ? <span className="faint"> ×{a.n}</span> : null}</div><div className="faint">{time(a.timestamp)}{a.symbol ? ` · ${a.symbol}` : ""}</div></div>
            </div>
          ))}
        </div>
      ) : <div className="small faint">No alerts. You'll see stops, pauses, risk blocks and losing streaks here.</div>}
    </Card>
  );
}

/** Small Q&A with the agent (POST /api/llm/ask) plus a one-line state summary (GET /api/llm/summary). */
export function AskAgent({ tick }: { tick: number }) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [chat, setChat] = useState<{ q: string; a: string }[]>([]);
  const [summary, setSummary] = useState<string | null>(null);
  useEffect(() => { if (tick % 5 === 0 || summary == null) api.llmSummary().then((r) => setSummary(r.summary)).catch(() => setSummary("")); }, [tick]); // eslint-disable-line
  const ask = async (text = q) => {
    if (!text.trim()) return;
    setBusy(true);
    try { const r = await api.llmAsk(text); setChat((c) => [...c.slice(-4), { q: text, a: r.answer }]); setQ(""); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); } finally { setBusy(false); }
  };
  return (
    <Card title="✨ Ask the agent">
      {summary ? <div className="summary-box small" style={{ marginBottom: 10 }}>{summary}</div> : summary == null ? <Skeleton h={40} style={{ marginBottom: 10 }} /> : null}
      <div className="stack" style={{ gap: 8, marginBottom: chat.length ? 10 : 0, maxHeight: 240, overflow: "auto" }}>
        {chat.map((m, i) => (
          <div key={i} className="small">
            <div className="muted">You: {m.q}</div>
            <div style={{ marginTop: 3 }}>{m.a}</div>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <input type="text" value={q} placeholder="Why did you stay out?" onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()} />
        <button className="btn" disabled={busy || !q.trim()} onClick={() => ask()}>{busy ? <span className="spin" /> : "Ask"}</button>
      </div>
      <div className="chips" style={{ marginTop: 8 }}>
        {["Why the last decision?", "What's the regime?", "How is PnL?"].map((s) => <button key={s} className="chip pass" style={{ cursor: "pointer", background: "transparent", color: "var(--muted)", borderColor: "var(--border-strong)" }} onClick={() => ask(s)}>{s}</button>)}
      </div>
    </Card>
  );
}

import { useEffect, useState } from "react";
import type { StatusSnapshot } from "../contracts";
import { api } from "../api";
import type { LoopState } from "../api/types";
import { ModeBadge } from "../components/Badges";
import { Card, NumberInput, Toggle, useToast } from "../components/ui";

interface Props { status: StatusSnapshot | null; onChange: () => void; tick: number }

/** Confirmation mode and the decision loop (start/stop/interval/tick once). Pause/resume/e-stop live in the global StatusBar. */
export function Controls({ status, onChange, tick }: Props) {
  const c = status?.control;
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [loop, setLoop] = useState<LoopState | null>(null);
  const [interval, setIntervalS] = useState<number | null>(null);

  useEffect(() => { api.getLoop().then(setLoop).catch(() => setLoop(null)); }, [tick, status?.agent_mode]);

  const run = async <T,>(fn: () => Promise<T>, okMsg?: string): Promise<T | undefined> => {
    setBusy(true);
    try { const r = await fn(); onChange(); if (okMsg) toast(okMsg, "success"); return r; }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); return undefined; }
    finally { setBusy(false); }
  };
  return (
    <Card title="Decision loop & confirmation" extra={<ModeBadge mode={c?.trading_mode} />}>
      <div className="small muted" style={{ marginBottom: 10 }}>Pause, Resume and Emergency stop are in the status bar at the top of every page.</div>
      <div className="rule on">
        <Toggle checked={!!c?.confirmation_mode} disabled={busy} label="Confirmation mode"
          onChange={(v) => run(() => api.setConfirmationMode(v), v ? "Confirmation mode on: entries wait for you" : "Auto-execute on")} />
        <div className="rule-text"><div className="rule-title">Confirmation mode</div><div className="rule-hint">Ask me before every entry (protective exits always run)</div></div>
      </div>

      <div className="group" style={{ margin: "12px -18px -18px", padding: "16px 18px 18px" }}>
        <div className="group-title">Decision loop
          <span className="count">{loop ? (loop.running ? <span className="pill ok"><span className="live-dot" />every {loop.interval_seconds}s</span> : <span className="pill">stopped</span>) : null}</span>
        </div>
        <div className="small muted" style={{ marginBottom: 10 }}>
          {loop ? <>{loop.ticks} ticks{loop.replay_finished ? " · SIM replay finished" : ""}</> : "Loop state unavailable"}
        </div>
        <div className="btn-row">
          {loop?.running
            ? <button className="btn ghost" style={{ flex: 1 }} disabled={busy} onClick={async () => { const l = await run(() => api.stopLoop(), "Loop stopped"); if (l) setLoop(l); }}>■ Stop loop</button>
            : <button className="btn ghost" style={{ flex: 1 }} disabled={busy || !status?.agent_mode} title={status?.agent_mode ? "Run a cycle every interval" : "Activate a manual strategy or auto config first"} onClick={async () => { const l = await run(() => api.startLoop(interval ?? undefined), "Loop started"); if (l) setLoop(l); }}>▶ Start loop</button>}
          <NumberInput value={interval ?? loop?.interval_seconds ?? 5} min={1} suffix="s" width={84} onChange={(v) => setIntervalS(v)} />
          <button className="btn ghost" disabled={busy || !status?.agent_mode} title={status?.agent_mode ? "Run one cycle now" : "Activate an agent first"}
            onClick={async () => { const d = await run(() => api.tick()); if (d) toast(`Tick: ${d.length ? d.map((x) => x.action.replace("_", " ")).join(", ") : "no decision"}`, "info"); }}>Tick once</button>
        </div>
      </div>

    </Card>
  );
}

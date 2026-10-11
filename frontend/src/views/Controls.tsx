import { useEffect, useState } from "react";
import type { StatusSnapshot } from "../contracts";
import { api } from "../api";
import type { LoopState } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ModeBadge } from "../components/Badges";
import { Card, NumberInput, Toggle, useToast } from "../components/ui";
import { controlReason } from "../lib/strategy";

interface Props { status: StatusSnapshot | null; onChange: () => void; tick: number }

/** Pause/resume, confirmation mode, decision loop, emergency stop (manual override = close buttons on positions). */
export function Controls({ status, onChange, tick }: Props) {
  const c = status?.control;
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [askStop, setAskStop] = useState(false);
  const [askReset, setAskReset] = useState(false);
  const [flatten, setFlatten] = useState(true);
  const [loop, setLoop] = useState<LoopState | null>(null);
  const [interval, setIntervalS] = useState<number | null>(null);

  useEffect(() => { api.getLoop().then(setLoop).catch(() => setLoop(null)); }, [tick, status?.agent_mode]);

  const run = async <T,>(fn: () => Promise<T>, okMsg?: string): Promise<T | undefined> => {
    setBusy(true);
    try { const r = await fn(); onChange(); if (okMsg) toast(okMsg, "success"); return r; }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); return undefined; }
    finally { setBusy(false); }
  };
  const state = c?.emergency_stopped ? <span className="pill bad"><span className="dot" />Emergency stop</span>
    : c?.paused ? <span className="pill warn"><span className="dot" />Paused</span>
    : <span className="pill ok"><span className="live-dot" style={{ marginRight: 2 }} />Running</span>;

  return (
    <Card title="Controls" extra={<ModeBadge mode={c?.trading_mode} />}>
      <div className="kv" style={{ marginBottom: 14 }}>
        <span className="k">Risk layer</span><span>{state}</span>
        {controlReason(c?.reason) && <><span className="k">Note</span><span className="small">{controlReason(c?.reason)}</span></>}
      </div>
      <div className="field-grid">
        {c?.paused
          ? <button className="btn green" disabled={busy} onClick={() => run(() => api.resume(), "Agent resumed")}>▶ Resume</button>
          : <button className="btn amber" disabled={busy} onClick={() => run(() => api.pause("Paused by user"), "Agent paused")}>❚❚ Pause</button>}
        {c?.emergency_stopped
          ? <button className="btn ghost" style={{ borderColor: "rgba(239,68,68,.5)" }} disabled={busy} onClick={() => setAskReset(true)}>↺ Reset e-stop</button>
          : <button className="btn red" disabled={busy} onClick={() => setAskStop(true)}>■ Emergency stop</button>}
      </div>
      <div className="rule on" style={{ marginTop: 10 }}>
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

      {askReset && (
        <ConfirmDialog title="Reset emergency stop?" confirmLabel="Reset emergency stop" busy={busy}
          onCancel={() => setAskReset(false)} onConfirm={() => run(() => api.resetEmergencyStop(), "Emergency stop reset").then(() => setAskReset(false))}>
          New entries become possible again on the next cycle{c?.paused ? " once you also resume (the agent is still paused)" : ""}.
          Make sure the reason for the stop is resolved first.
        </ConfirmDialog>
      )}
      {askStop && (
        <ConfirmDialog title="Trigger emergency stop?" danger confirmLabel="Stop everything" busy={busy}
          onCancel={() => setAskStop(false)} onConfirm={() => run(() => api.emergencyStop("Emergency stop by user", flatten), "Emergency stop engaged").then(() => setAskStop(false))}>
          Every cycle becomes STAY OUT (no new buys) and pending confirmations are rejected ({c?.trading_mode ?? "SIM"} mode).{" "}
          {flatten ? "All open positions are closed immediately at the last price." : "Open positions are closed on the next bar at that bar's close (exit reason EMERGENCY_STOP)."}{" "}
          Clear it later with “Reset e-stop” (Resume does not clear it).
          <div className="rule on" style={{ marginTop: 12 }}>
            <Toggle checked={flatten} onChange={setFlatten} label="Close all positions" />
            <div className="rule-text"><div className="rule-title">Close all positions now (flatten)</div><div className="rule-hint">{flatten ? "Immediately, at the last price" : "Off: positions close on the next bar at the bar close"}</div></div>
          </div>
        </ConfirmDialog>
      )}
    </Card>
  );
}

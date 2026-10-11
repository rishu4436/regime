// Global status bar shown above every tab: agent state + mode, Pause/Resume, Emergency stop (one shared confirm
// dialog), Reconnect wallet when the Agentic Wallet session is inactive in LIVE, and every wallet/limits risk warning.
import { useState, type ReactNode } from "react";
import type { StatusSnapshot } from "../contracts";
import { api } from "../api";
import { ago, limitsOf, useLive } from "../lib/live";
import { controlReason, strategyLabel } from "../lib/strategy";
import { usd } from "../lib/format";
import { ModeBadge } from "./Badges";
import { ConfirmDialog } from "./ConfirmDialog";
import { AUTO_STOP, WARNING_TEXT } from "./Live";
import { Toggle, useToast } from "./ui";
import { useWalletConnect } from "./WalletConnect";

/** Agent state shared by the bar and the Dashboard header so both always say the same thing. */
export function agentState(status: StatusSnapshot | null, loopRunning: boolean | null | undefined, stale: boolean): { label: string; short: string; tone: "ok" | "warn" | "bad" | ""; icon: string } {
  if (!status) return { label: "Connecting…", short: "Connecting", tone: "", icon: "○" };
  const c = status.control;
  const autoStop = c.emergency_stopped && AUTO_STOP.test(c.reason ?? "");
  if (c.emergency_stopped) return { label: autoStop ? "Auto-stopped: daily loss cap hit" : "Emergency stopped", short: autoStop ? "Auto-stopped" : "Stopped", tone: "bad", icon: "■" };
  if (c.paused) return { label: "Paused by kill switch", short: "Paused", tone: "warn", icon: "❚❚" };
  if (!status.agent_mode) return { label: "Idle: no agent active", short: "Idle", tone: "", icon: "○" };
  if (loopRunning === false) return { label: "Agent active, loop stopped", short: "Loop stopped", tone: "warn", icon: "○" };
  if (stale) return { label: "Heartbeat lost: loop not ticking", short: "No heartbeat", tone: "bad", icon: "!" };
  if (c.confirmation_mode) return { label: "Running (confirmation mode on)", short: "Running · asks first", tone: "ok", icon: "●" };
  return { label: "Agent running autonomously", short: "Running", tone: "ok", icon: "●" };
}

/** Pause / Resume / Emergency stop / Reset e-stop actions with their confirm dialogs (render `dialogs` once). */
export function useAgentControls(status: StatusSnapshot | null, refresh: () => void) {
  const toast = useToast();
  const { isLive, refresh: refreshLive } = useLive();
  const [ask, setAsk] = useState<null | "stop" | "reset">(null);
  const [busy, setBusy] = useState(false);
  const [flatten, setFlatten] = useState(false);
  const c = status?.control;
  const autoStop = !!c?.emergency_stopped && AUTO_STOP.test(c?.reason ?? "");
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    try { await fn(); toast(msg, "success"); refresh(); refreshLive(); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    finally { setBusy(false); setAsk(null); }
  };
  const openStop = () => { setFlatten(false); setAsk("stop"); };
  const buttons = !c ? null : <>
    {c.paused
      ? <button className="btn green sm" disabled={busy} onClick={() => run(() => api.resume(), "Agent resumed")}>▶ Resume</button>
      : <button className="btn amber sm" disabled={busy || c.emergency_stopped} onClick={() => run(() => api.pause("Paused by user (kill switch)"), "Agent paused")}>❚❚ Pause</button>}
    {c.emergency_stopped
      ? <button className="btn ghost sm" disabled={busy} onClick={() => setAsk("reset")}>↺ Reset e-stop</button>
      : <button className="btn red sm" disabled={busy} onClick={openStop}>■ Emergency stop</button>}
  </>;
  const dialogs = !c ? null : <>
    {ask === "stop" && (
      <ConfirmDialog title="Emergency stop?" danger confirmLabel="Stop the agent" busy={busy} onCancel={() => setAsk(null)}
        onConfirm={() => run(() => api.emergencyStop("Emergency stop by user", flatten), "Emergency stop engaged")}>
        The agent stops opening trades immediately and pending confirmations are rejected ({c.trading_mode} mode).{" "}
        {flatten ? "All open positions are closed right away at the last price." : "Open positions are closed on the next bar at that bar's close, logged as an emergency stop."}{" "}
        Clear it later with “Reset e-stop” (Resume does not clear it).
        <div className="rule on" style={{ marginTop: 12 }}>
          <Toggle checked={flatten} onChange={setFlatten} label="Close all positions now" />
          <div className="rule-text"><div className="rule-title">Close all positions now</div><div className="rule-hint">{flatten ? "Right away, at the last price" : "Off: positions close on the next bar at the bar close"}</div></div>
        </div>
      </ConfirmDialog>
    )}
    {ask === "reset" && (
      <ConfirmDialog title="Reset the emergency stop?" confirmLabel="Reset and resume trading" busy={busy} onCancel={() => setAsk(null)}
        onConfirm={() => run(() => api.resetEmergencyStop(), "Emergency stop reset")}>
        {autoStop ? "It was triggered by the daily loss cap. " : ""}New entries become possible again on the next cycle{c.paused ? " once you also resume (the agent is still paused)" : ""}{isLive ? ", with real funds" : ""}.
        Make sure the reason for the stop is resolved first.
      </ConfirmDialog>
    )}
  </>;
  return { buttons, dialogs, busy };
}

export interface RiskWarning { code: string; text: ReactNode; tone: "red" | "amber" }
/** Every wallet/limits warning in plain language. exit_quota_shortfall is red (stop-loss sells could be refused). */
export function useRiskWarnings(): RiskWarning[] {
  const { wallet, isLive, now } = useLive();
  if (!wallet) return [];
  const L = limitsOf(wallet);
  const out: RiskWarning[] = [];
  const seen = new Set<string>();
  for (const w of L.warnings ?? []) {
    if (!w) continue;
    const code = typeof w === "string" ? w : w.code;
    const msg = typeof w === "string" ? null : w.message ?? null;
    const key = `${code}|${msg ?? ""}`;
    if (seen.has(key)) continue; seen.add(key); seen.add(code);
    const label = WARNING_TEXT[code];
    const text = code === "exit_quota_shortfall"
      ? <><b>Stop-loss sells could be refused:</b> {label}{msg && msg !== code ? <span className="muted"> ({msg})</span> : null}</>
      : label ? <>{label}{msg && msg !== code && msg !== label ? <span className="muted"> ({msg})</span> : null}</> : <>{msg ?? code.replace(/_/g, " ")}</>;
    out.push({ code, text, tone: code === "exit_quota_shortfall" ? "red" : "amber" });
  }
  // derived when the backend doesn't send them as warnings
  if (isLive && !seen.has("wallet_too_small") && L.min_wallet_usdt != null) {
    const u = wallet.balances.find((b) => (b.asset ?? b.symbol) === "USDT");
    const amt = u ? u.value_usdt ?? u.usd_value ?? u.amount ?? u.quantity ?? null : null;
    if (amt != null && amt < L.min_wallet_usdt) out.push({ code: "wallet_too_small", tone: "amber", text: <>{WARNING_TEXT.wallet_too_small} <span className="muted">({usd(amt)} USDT, needs about {usd(L.min_wallet_usdt, 0)})</span></> });
  }
  if (wallet.stale && !seen.has("wallet_limits_stale")) {
    const read = wallet.read_at ?? wallet.updated_at;
    const age = wallet.age_s != null ? `read ${Math.round(wallet.age_s / 60)} min ago` : read ? `read ${ago(Date.parse(read), now)}` : null;
    out.push({ code: "wallet_stale", tone: "amber", text: <>Wallet balances and limits are out of date{age ? ` (${age})` : ""}, so caps may be checked against old numbers.</> });
  }
  return out;
}

/** Slim sticky bar above every tab. */
export function StatusBar({ status, refresh }: { status: StatusSnapshot | null; refresh: () => void }) {
  const { isLive, wallet, loop, hb, system } = useLive();
  const { open } = useWalletConnect();
  const { buttons, dialogs } = useAgentControls(status, refresh);
  const warnings = useRiskWarnings();
  const st = agentState(status, loop?.running, hb.stale);
  const mode = isLive ? "LIVE" : status?.control.trading_mode ?? system?.trading_mode;
  const needsWallet = isLive && !!wallet && (!!wallet.session_expired || wallet.connected === false);
  const c = status?.control;
  const sub = status?.agent_mode ? `${status.agent_mode} · ${strategyLabel(status.active_strategy)} · ${status.active_symbol}` : null;
  const note = c && (c.emergency_stopped || c.paused) ? controlReason(c.reason) : null;
  return (
    <>
      <div className={`statusbar ${st.tone}`} role="region" aria-label="Agent status and controls">
        <span className={`pill ${st.tone}`}>{st.tone === "ok" ? <span className="live-dot" /> : <span className="dot" />}{st.short}</span>
        <ModeBadge mode={mode} />
        <span className="statusbar-text small muted" title={[st.label, sub, note].filter(Boolean).join(" · ")}>
          {sub ?? "No strategy active"}{note ? ` · ${note}` : ""}
        </span>
        <span className="statusbar-actions">
          {needsWallet && <button className="btn danger sm" onClick={open} title="The Agentic Wallet session is inactive: scan the QR in the Binance App">⟳ Reconnect wallet</button>}
          {buttons}
        </span>
      </div>
      {warnings.length > 0 && (
        <div className="risk-warnings" role="alert">
          {warnings.map((w, i) => <div key={`${w.code}-${i}`} className={`risk-warning ${w.tone}`}><span aria-hidden>{w.tone === "red" ? "⛔" : "⚠"}</span><span>{w.text}</span></div>)}
        </div>
      )}
      {dialogs}
    </>
  );
}

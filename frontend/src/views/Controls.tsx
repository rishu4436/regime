import { useState } from "react";
import type { OrderSide, StatusSnapshot } from "../contracts";
import { api } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ModeBadge } from "../components/Badges";
import { Card, NumberInput, Segmented, Toggle, useToast } from "../components/ui";
import { usd } from "../lib/format";
import { TOKENS } from "../lib/strategy";

interface Props { status: StatusSnapshot | null; onChange: () => void }

/** Pause/resume, confirmation mode, manual override and emergency stop. */
export function Controls({ status, onChange }: Props) {
  const c = status?.control;
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [askStop, setAskStop] = useState(false);
  const [askOvr, setAskOvr] = useState(false);
  const [ovr, setOvr] = useState<{ side: OrderSide; symbol: string; notional: number }>({ side: "BUY", symbol: status?.active_symbol ?? "NVDAon", notional: 500 });

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await fn(); onChange(); if (ok) toast(ok, "success"); return true; }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); return false; }
    finally { setBusy(false); }
  };
  const pos = status?.portfolio.positions?.find((p) => p.symbol === ovr.symbol);
  const state = c?.emergency_stopped ? <span className="pill bad"><span className="dot" />Emergency stop</span>
    : c?.paused ? <span className="pill warn"><span className="dot" />Paused</span>
    : <span className="pill ok"><span className="live-dot" style={{ marginRight: 2 }} />Running</span>;

  return (
    <Card title="Controls" extra={<ModeBadge mode={c?.trading_mode} />}>
      <div className="kv" style={{ marginBottom: 14 }}>
        <span className="k">Agent</span><span>{state}</span>
        {c?.reason && <><span className="k">Note</span><span className="small">{c.reason}</span></>}
      </div>
      <div className="field-grid">
        {c?.paused || c?.emergency_stopped
          ? <button className="btn green" disabled={busy} onClick={() => run(() => api.resume(), "Agent resumed")}>▶ Resume</button>
          : <button className="btn amber" disabled={busy} onClick={() => run(() => api.pause("Paused by user"), "Agent paused")}>❚❚ Pause</button>}
        <button className="btn red" disabled={busy || c?.emergency_stopped} onClick={() => setAskStop(true)}>■ Emergency stop</button>
      </div>
      <div className="rule on" style={{ marginTop: 10 }}>
        <Toggle checked={!!c?.confirmation_mode} disabled={busy} label="Confirmation mode"
          onChange={(v) => run(() => api.setConfirmationMode(v), v ? "Confirmation mode on: trades wait for you" : "Auto-execute on")} />
        <div className="rule-text"><div className="rule-title">Confirmation mode</div><div className="rule-hint">Ask me before every trade</div></div>
      </div>

      <div className="group" style={{ margin: "12px -18px -18px", padding: "16px 18px 18px" }}>
        <div className="group-title">Manual override</div>
        <div style={{ display: "grid", gap: 10 }}>
          <Segmented value={ovr.side} onChange={(side) => setOvr({ ...ovr, side })}
            options={[{ value: "BUY", label: "Buy" }, { value: "SELL", label: "Sell" }]} />
          <div className="field-grid">
            <div><span className="lbl">Token</span>
              <select value={ovr.symbol} onChange={(e) => setOvr({ ...ovr, symbol: e.target.value })}>{TOKENS.map((t) => <option key={t.symbol}>{t.symbol}</option>)}</select></div>
            <div><span className="lbl">Amount</span><NumberInput value={ovr.notional} min={1} step={50} suffix="USDT" onChange={(v) => setOvr({ ...ovr, notional: v ?? 0 })} /></div>
          </div>
          <div className="btn-row">
            <button className="btn ghost" style={{ flex: 1 }} disabled={busy || c?.emergency_stopped || ovr.notional <= 0} onClick={() => setAskOvr(true)}>Send {ovr.side.toLowerCase()} order</button>
            {pos && <button className="btn ghost" disabled={busy} onClick={() => run(() => api.override({ symbol: pos.symbol, side: "SELL", notional: (pos.current_price ?? pos.avg_entry_price) * pos.quantity, quantity: pos.quantity }), `${pos.symbol} position closed`)}>Close {pos.symbol}</button>}
          </div>
        </div>
      </div>

      {askStop && (
        <ConfirmDialog title="Trigger emergency stop?" danger confirmLabel="Stop everything" busy={busy}
          onCancel={() => setAskStop(false)} onConfirm={() => run(() => api.emergencyStop("Emergency stop by user"), "Emergency stop engaged").then(() => setAskStop(false))}>
          The agent halts immediately, pending trades are cancelled and all open positions are closed at market
          ({c?.trading_mode ?? "SIM"} mode). You can resume afterwards.
        </ConfirmDialog>
      )}
      {askOvr && (
        <ConfirmDialog title="Send manual order?" confirmLabel={`${ovr.side} ${ovr.symbol}`} busy={busy}
          onCancel={() => setAskOvr(false)} onConfirm={() => run(() => api.override({ symbol: ovr.symbol, side: ovr.side, notional: ovr.notional }), `${ovr.side} ${ovr.symbol} sent`).then(() => setAskOvr(false))}>
          {ovr.side} {usd(ovr.notional)} of {ovr.symbol} in <b>{c?.trading_mode ?? "SIM"}</b> mode, bypassing the strategy (risk limits still apply).
        </ConfirmDialog>
      )}
    </Card>
  );
}

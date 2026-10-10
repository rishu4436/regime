import { useEffect, useMemo, useState } from "react";
import type { Candle, Decision, StatusSnapshot } from "../contracts";
import { api } from "../api";
import { ActionBadge, RegimeBadge } from "../components/Badges";
import { PriceChart, type TradeMarker } from "../components/Charts";
import { Card, Segmented, Skeleton, StatCard, useToast } from "../components/ui";
import { cls, humanize, num, time, usd } from "../lib/format";
import { TOKENS } from "../lib/strategy";
import { Controls } from "./Controls";

interface Props { status: StatusSnapshot | null; refresh: () => void; tick: number; go: (tab: "manual" | "auto") => void }

export function Dashboard({ status, refresh, tick, go }: Props) {
  const toast = useToast();
  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"ALL" | "TRADES">("ALL");
  const symbol = status?.active_symbol ?? "NVDAon";

  useEffect(() => { api.getCandles(symbol, "1h", 150).then(setCandles).catch(() => setCandles([])); }, [symbol, tick]);

  const pf = status?.portfolio;
  const unreal = pf ? (pf.positions ?? []).reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0) : null;
  const totalPnl = pf ? pf.equity - pf.starting_capital : null;
  const decisions = status?.recent_decisions ?? [];
  const pending = decisions.filter((d) => d.execution?.status === "PENDING_CONFIRMATION");
  const shown = filter === "ALL" ? decisions : decisions.filter((d) => d.action !== "STAY_OUT");
  const last = status?.last_decision;
  const lastPx = candles?.[candles.length - 1]?.close, firstPx = candles?.[0]?.close;

  const markers = useMemo<TradeMarker[]>(() => decisions
    .filter((d) => d.symbol === symbol && d.execution?.status === "FILLED" && (d.action === "BUY" || d.action === "SELL") && d.timestamp)
    .map((d) => ({ time: d.timestamp!, type: d.action as "BUY" | "SELL", text: d.action === "BUY" ? "BUY" : "SELL" })), [decisions, symbol]);

  const act = async (id: string, ok: boolean) => {
    setBusy(id);
    try { await (ok ? api.confirmDecision(id) : api.rejectDecision(id)); toast(ok ? "Trade confirmed and sent" : "Trade rejected", ok ? "success" : "info"); refresh(); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); } finally { setBusy(null); }
  };

  return (
    <>
      {status?.control.emergency_stopped && <div className="banner red"><b>■ Emergency stop active</b><span className="grow">{status.control.reason}</span><span className="small muted">Resume from Controls when ready</span></div>}
      {status && !status.agent_mode && (
        <div className="banner blue">
          <span className="grow">No strategy is running. Build rules in <b>Manual Builder</b> or let the agent choose in <b>Auto Mode</b>.</span>
          <button className="btn ghost" onClick={() => go("manual")}>Open builder</button>
          <button className="btn" onClick={() => api.activateAuto({ symbol: "NVDAon", style: "BALANCED", capital: 10000, activation: "CONFIRMATION" }).then(() => { toast("Auto mode started (Balanced, NVDAon)", "success"); refresh(); })}>⚡ Quick start Auto</button>
        </div>
      )}

      {pending.map((d) => (
        <Card key={d.id} className="glow" title={<>⏳ Trade awaiting your confirmation <ActionBadge action={d.action} /></>}
          extra={<span className="muted small">{time(d.timestamp)}</span>}>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 10 }}>
            <div><div className="lbl">Token</div><b>{d.symbol}</b></div>
            <div><div className="lbl">Amount</div><b>{usd(d.signal?.proposed_notional)}</b></div>
            <div><div className="lbl">Price</div><b>~{usd(d.execution?.avg_price ?? d.signal?.price)}</b></div>
            <div><div className="lbl">Strategy</div><b>{d.strategy}</b></div>
          </div>
          <div className="summary-box" style={{ marginBottom: 12 }}>{d.explanation ?? d.reason}</div>
          <div className="btn-row">
            <button className="btn green" disabled={busy === d.id} onClick={() => act(d.id!, true)}>✓ Confirm trade</button>
            <button className="btn ghost" disabled={busy === d.id} onClick={() => act(d.id!, false)}>Reject</button>
          </div>
        </Card>
      ))}
      {pending.length > 0 && <div style={{ height: 16 }} />}

      <div className="stats">
        <StatCard label="Equity" value={pf?.equity} format={(v) => usd(v)} delta={pf ? (totalPnl! / pf.starting_capital) * 100 : null} deltaLabel="all time" />
        <StatCard label="Total PnL" value={totalPnl} format={(v) => usd(v)} tone="auto" />
        <StatCard label="Realized" value={pf?.realized_pnl} format={(v) => usd(v)} tone="auto" />
        <StatCard label="Unrealized" value={unreal} format={(v) => usd(v)} tone="auto" />
        <StatCard label="Cash" value={pf?.cash} format={(v) => usd(v)} />
        <StatCard label="Open positions" value={pf ? pf.positions?.length ?? 0 : null} format={(v) => Math.round(v).toString()} />
      </div>

      <div className="split">
        <div className="stack">
          <Card title={<>{symbol} <span className="muted small" style={{ fontWeight: 400 }}>{TOKENS.find((t) => t.symbol === symbol)?.label.split(" ")[1]?.replace(/[()]/g, "")} · 1h</span></>}
            extra={<>
              {lastPx != null && firstPx != null && <span className={`delta ${lastPx >= firstPx ? "pos" : "neg"}`}>{lastPx >= firstPx ? "▲" : "▼"} {Math.abs((lastPx / firstPx - 1) * 100).toFixed(2)}%</span>}
              <b style={{ fontSize: "1.1rem" }}>{usd(lastPx)}</b></>}>
            {candles ? <PriceChart candles={candles} markers={markers} height={320} /> : <Skeleton h={320} />}
          </Card>
          <Card title="Last decision" extra={last && <ActionBadge action={last.action} />}>
            {!status ? <><Skeleton h={14} /><Skeleton h={14} w="70%" style={{ marginTop: 8 }} /></> : last ? <DecisionDetail d={last} /> : <div className="empty">No decisions yet — the agent logs one every bar once a strategy is active.</div>}
          </Card>
          <Card title="Open positions" pad={false}>
            <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
              <table>
                <thead><tr><th>Token</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Price</th><th className="r">uPnL</th><th>Stops</th><th>Strategy</th></tr></thead>
                <tbody>
                  {(pf?.positions ?? []).map((p) => (
                    <tr key={p.symbol}>
                      <td><b>{p.symbol}</b></td><td className="r">{num(p.quantity)}</td><td className="r">{usd(p.avg_entry_price)}</td><td className="r">{usd(p.current_price)}</td>
                      <td className={`r ${cls(p.unrealized_pnl)}`}>{usd(p.unrealized_pnl)}</td>
                      <td className="small">SL {usd(p.stop_loss_price)} · TP {usd(p.take_profit_price)}{p.trailing_stop_pct ? ` · trail ${p.trailing_stop_pct}%` : ""}</td>
                      <td className="small">{p.strategy}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!pf?.positions?.length && <div className="empty">No open positions</div>}
            </div>
          </Card>
        </div>
        <div className="stack">
          <Card title="Agent" extra={status?.agent_mode && <span className="pill">{status.agent_mode}</span>}>
            {!status ? <Skeleton h={140} /> : (
              <div className="kv">
                <span className="k">Market regime</span><span><RegimeBadge regime={status.current_regime} /></span>
                <span className="k">Active strategy</span><span><b>{status.active_strategy ?? "—"}</b></span>
                <span className="k">Token</span><span>{symbol}</span>
                <span className="k">Trades today</span><span>{pf?.trades_today ?? 0}</span>
                <span className="k">Daily PnL</span><span className={cls(pf?.daily_pnl)}>{usd(pf?.daily_pnl)}</span>
                <span className="k">Updated</span><span className="small muted">{time(status.timestamp)}</span>
              </div>
            )}
          </Card>
          <Controls status={status} onChange={refresh} />
        </div>
      </div>

      <div style={{ height: 16 }} />
      <Card title={<>Decision log <span className="muted small" style={{ fontWeight: 400 }}>{shown.length}</span></>}
        extra={<Segmented size="sm" value={filter} onChange={setFilter} options={[{ value: "ALL", label: "All" }, { value: "TRADES", label: "Trades only" }]} />} pad={false}>
        <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
          <table>
            <thead><tr><th>Time</th><th>Action</th><th>Token</th><th>Regime</th><th>Strategy</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>
              {shown.map((d) => (
                <tr key={d.id}>
                  <td className="small muted">{time(d.timestamp)}</td><td><ActionBadge action={d.action} /></td><td>{d.symbol}</td>
                  <td><RegimeBadge regime={d.regime} /></td><td className="small">{d.strategy}</td>
                  <td className="small">{d.execution ? humanize(d.execution.status) : "—"}</td>
                  <td className="wrap small">{d.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!shown.length && <div className="empty">Nothing logged yet</div>}
        </div>
      </Card>
    </>
  );
}

function DecisionDetail({ d }: { d: Decision }) {
  const v = d.risk_verdict;
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="kv">
        <span className="k">When</span><span className="small">{time(d.timestamp)} · {d.mode} · {d.agent_mode}</span>
        <span className="k">Token / strategy</span><span>{d.symbol} · <b>{d.strategy}</b></span>
        <span className="k">Regime</span><span><RegimeBadge regime={d.regime} /></span>
        <span className="k">Reason</span><span>{d.reason}</span>
        {d.signal?.conditions_met?.length ? <><span className="k">Met</span><span className="small pos">{d.signal.conditions_met.join(" · ")}</span></> : null}
        {d.signal?.conditions_failed?.length ? <><span className="k">Not met</span><span className="small neg">{d.signal.conditions_failed.join(" · ")}</span></> : null}
        {v && <><span className="k">Risk check</span><span>{v.approved ? <span className="pill ok">approved</span> : <span className="pill bad">blocked</span>} <span className="small muted">{v.reasons?.join("; ")}{v.adjusted_notional ? ` ${usd(v.adjusted_notional)}` : ""}</span></span></>}
        {d.execution && <><span className="k">Execution</span><span className="small">{humanize(d.execution.status)} {d.execution.avg_price ? `@ ${usd(d.execution.avg_price)}` : ""} {d.execution.error ?? ""}</span></>}
      </div>
      {d.explanation && d.explanation !== d.reason && <div className="summary-box">{d.explanation}</div>}
    </div>
  );
}

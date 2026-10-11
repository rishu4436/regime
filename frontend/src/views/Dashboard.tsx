import { useEffect, useMemo, useState } from "react";
import type { Candle, Decision, StatusSnapshot } from "../contracts";
import { api } from "../api";
import { ActionBadge, RegimeBadge } from "../components/Badges";
import { PriceChart, type TradeMarker } from "../components/Charts";
import { Card, Segmented, Skeleton, StatCard, useToast } from "../components/ui";
import { cls, humanize, num, time, usd } from "../lib/format";
import { conditionLabel, controlReason, EXIT_LABEL, heldExit, isExitQueued, isPendingPos, strategyLabel, TOKENS } from "../lib/strategy";
import { Controls } from "./Controls";
import { AlertsPanel, AskAgent } from "./Insights";
import { ApiError, type PerformanceSummary } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ActivityFeed, AgentIdentityCard, AutonomyMonitor, CapsPanel, LiveGateCard, TxLink, WalletPanel } from "../components/Live";
import { useLive } from "../lib/live";

interface Props { status: StatusSnapshot | null; refresh: () => void; tick: number; alertsVersion: number; go: (tab: "manual" | "auto") => void }

/** Activate, then make sure the decision loop runs (the backend does not auto-start it). */
export async function ensureLoop() {
  const l = await api.getLoop();
  if (!l.running) await api.startLoop();
}

export function Dashboard({ status, refresh, tick, alertsVersion, go }: Props) {
  const toast = useToast();
  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"ALL" | "TRADES">("ALL");
  const [perf, setPerf] = useState<PerformanceSummary | null>(null);
  const [closeTarget, setCloseTarget] = useState<{ kind: "lot"; id: string; symbol: string } | { kind: "all"; symbol: string } | null>(null);
  const [askReset, setAskReset] = useState(false);
  useEffect(() => { api.getPerformance().then(setPerf).catch(() => setPerf(null)); }, [tick]);
  const symbol = status?.active_symbol ?? "NVDAB";

  // default token NVDAB; a backend whose token list lacks it falls back to NVDAon for the idle chart
  useEffect(() => { api.getCandles(symbol, "1h", 150).then(setCandles).catch(() => status?.active_symbol ? setCandles([]) : api.getCandles("NVDAon", "1h", 150).then(setCandles).catch(() => setCandles([]))); }, [symbol, tick]);

  const pf = status?.portfolio;
  const unreal = pf ? (pf.positions ?? []).reduce((a, p) => a + (isPendingPos(p) ? 0 : p.unrealized_pnl ?? 0), 0) : null;
  const totalPnl = pf ? pf.equity - pf.starting_capital : null;
  const decisions = status?.recent_decisions ?? [];
  // authoritative list from GET /api/confirmations (recent_decisions only holds the last 20); refreshed on every snapshot
  const [pendingList, setPendingList] = useState<Decision[] | null>(null);
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  useEffect(() => { api.getConfirmations().then(setPendingList).catch(() => setPendingList(null)); }, [status, tick]);
  // approval cards only exist when confirmation mode is deliberately on (the agent is autonomous otherwise)
  const pending = status?.control.confirmation_mode ? (pendingList ?? decisions.filter((d) => d.execution?.status === "PENDING_CONFIRMATION")).filter((d) => !gone.has(d.id!)) : [];
  const { isLive } = useLive();
  const [feed, setFeed] = useState<Decision[]>([]);
  useEffect(() => { api.getDecisions(60).then(setFeed).catch(() => {}); }, [status, tick]);
  const shown = filter === "ALL" ? decisions : decisions.filter((d) => d.action !== "STAY_OUT");
  const last = status?.last_decision;
  const lastPx = candles?.[candles.length - 1]?.close, firstPx = candles?.[0]?.close;

  const markers = useMemo<TradeMarker[]>(() => decisions
    .filter((d) => d.symbol === symbol && d.execution?.status === "FILLED" && (d.action === "BUY" || d.action === "SELL") && d.timestamp)
    .map((d) => ({ time: d.timestamp!, type: d.action as "BUY" | "SELL", text: d.action === "BUY" ? "B" : "S" })), [decisions, symbol]);

  const lotCount = (sym: string) => (pf?.positions ?? []).filter((p) => p.symbol === sym).length;
  const act = async (id: string, ok: boolean) => {
    setBusy(id);
    try {
      const d = await (ok ? api.approve(id) : api.reject(id, "rejected in UI"));
      if (ok && d.execution?.status !== "FILLED") toast(`Trade ${d.execution?.status?.toLowerCase() ?? "not filled"}: ${d.execution?.error ?? "quote expired"}`, "warn");
      else toast(ok ? "Trade approved and executed" : "Trade rejected", ok ? "success" : "info");
      setGone((g) => new Set(g).add(id)); // handled: drop the card now, don't wait for the next snapshot
      refresh();
    }
    catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        // no longer pending: the agent was switched/deactivated, it expired, or it was handled elsewhere
        setGone((g) => new Set(g).add(id));
        toast(`This trade is no longer pending (${e.detail}). It was not executed.`, "warn");
        refresh();
      } else toast(e instanceof Error ? e.message : String(e), "error");
    } finally { setBusy(null); }
  };

  return (
    <>
      <AutonomyMonitor status={status} refresh={refresh} />
      {status?.control.emergency_stopped && !isLive && (
        <div className="banner red" role="alert">
          <span style={{ fontSize: "1.4rem" }}>■</span>
          <span className="grow">
            <b>Emergency stop active</b>{controlReason(status.control.reason) ? `: ${controlReason(status.control.reason)}` : ""}<br />
            <span className="small">Every cycle is <b>STAY OUT</b> (no new buys) and pending confirmations are rejected.{" "}
              {pf?.positions?.length ? <>The {pf.positions.length} open position{pf.positions.length > 1 ? "s close" : " closes"} on the next bar at that bar's close (exit reason EMERGENCY_STOP).</> : "Any positions left were closed with exit reason EMERGENCY_STOP."}</span>
          </span>
          <button className="btn ghost" onClick={() => setAskReset(true)}>↺ Reset emergency stop</button>
        </div>
      )}
      {status && !status.agent_mode && (
        <div className="banner blue">
          <span className="grow">No strategy is running. Build rules in <b>Manual Builder</b> or let the agent choose in <b>Auto Mode</b>.</span>
          <button className="btn ghost" onClick={() => go("manual")}>Open builder</button>
          <button className="btn" onClick={() => isLive ? go("auto") : api.activateAuto({ symbol: "NVDAB", style: "BALANCED", capital: 10000, activation: "CONFIRMATION" }).then(ensureLoop).then(() => { toast("Auto mode started (Balanced, NVDAB)", "success"); refresh(); }).catch((e) => toast(e instanceof Error ? e.message : String(e), "error"))}>⚡ Quick start Auto</button>
        </div>
      )}

      {pending.map((d) => (
        <Card key={d.id} className="glow" title={<>⏳ Trade awaiting your confirmation <ActionBadge action={d.action} /></>}
          extra={<span className="muted small">{time(d.timestamp)}</span>}>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 10 }}>
            <div><div className="lbl">Token</div><b>{d.symbol}</b></div>
            <div><div className="lbl">Amount</div><b>{usd(d.signal?.proposed_notional)}</b></div>
            <div><div className="lbl">Price</div><b>~{usd(d.execution?.avg_price ?? d.signal?.price)}</b></div>
            <div><div className="lbl">Strategy</div><b>{strategyLabel(d.strategy)}</b></div>
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
        <StatCard label="Win rate" value={perf ? perf.win_rate_pct : null} format={(v) => `${v.toFixed(0)}%`} />
        <StatCard label="Max drawdown" value={perf ? perf.max_drawdown_pct : null} format={(v) => `${v.toFixed(2)}%`} tone={perf && perf.max_drawdown_pct > 0 ? "neg" : undefined} />
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
          <Card title="Open positions" pad={false} extra={<>{[...new Set((pf?.positions ?? []).map((p) => p.symbol))].filter((sym) => lotCount(sym) > 1).map((sym) => (
            <button key={sym} className="btn ghost" style={{ padding: "4px 10px" }} onClick={() => setCloseTarget({ kind: "all", symbol: sym })}>Close all {sym}</button>))}</>}>
            <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
              <table>
                <thead><tr><th>Token</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Price</th><th className="r">uPnL</th><th>Stops</th><th>Strategy</th><th></th></tr></thead>
                <tbody>
                  {(pf?.positions ?? []).map((p, i) => (
                    <tr key={p.id ?? `${p.symbol}-${i}`} className={isPendingPos(p) ? "settling" : undefined}>
                      <td><b>{p.symbol}</b>{isPendingPos(p) && <span className="pill warn" style={{ marginLeft: 6 }} title={`In-flight buy${(p as { client_order_id?: string }).client_order_id ? ` · order ${(p as { client_order_id?: string }).client_order_id}` : ""}`}>⏳ Settling on-chain</span>}
                        {heldExit(p) && <span className="pill warn" style={{ marginLeft: 6 }} title={[heldExit(p)?.code, heldExit(p)?.reason, heldExit(p)?.source].filter(Boolean).join(" · ")}>Exit queued until fill</span>}
                        {p.id && <div className="faint small" title={p.id}>lot {p.id.slice(0, 6)}</div>}</td><td className="r">{num(p.quantity)}</td><td className="r">{usd(p.avg_entry_price)}</td><td className="r">{usd(p.current_price)}</td>
                      <td className={`r ${isPendingPos(p) ? "" : cls(p.unrealized_pnl)}`}>{isPendingPos(p) ? "—" : usd(p.unrealized_pnl)}</td>
                      <td className="small">SL {usd(p.stop_loss_price)} · TP {usd(p.take_profit_price)}{p.trailing_stop_pct ? ` · trail ${p.trailing_stop_pct}%` : ""}</td>
                      <td className="small trunc" title={p.strategy}>{p.strategy}</td>
                      <td><button className="btn ghost" style={{ padding: "4px 10px" }} disabled={isPendingPos(p)} title={isPendingPos(p) ? "Buy still settling on-chain: exit queued until fill" : "Close this lot at the last price"}
                        onClick={() => setCloseTarget(p.id ? { kind: "lot", id: p.id, symbol: p.symbol } : { kind: "all", symbol: p.symbol })}>Close</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!pf?.positions?.length && <div className="empty">No open positions</div>}
            </div>
          </Card>
        </div>
        <div className="stack">
          <Card title="Agent" extra={status?.agent_mode && <>
            <span className="pill">{status.agent_mode}</span>
            <button className="btn ghost" style={{ padding: "4px 10px" }} onClick={() => api.deactivate().then(() => { toast("Agent deactivated", "info"); refresh(); }).catch((e) => toast(e instanceof Error ? e.message : String(e), "error"))}>Deactivate</button></>}>
            {!status ? <Skeleton h={140} /> : (
              <div className="kv">
                <span className="k">Market regime</span><span><RegimeBadge regime={status.current_regime} /></span>
                <span className="k">Active strategy</span><span><b>{strategyLabel(status.active_strategy)}</b></span>
                <span className="k">Token</span><span>{symbol}</span>
                <span className="k">Trades today</span><span>{pf?.trades_today ?? 0}</span>
                <span className="k">Daily PnL</span><span className={cls(pf?.daily_pnl)}>{usd(pf?.daily_pnl)}</span>
                <span className="k">Updated</span><span className="small muted">{time(status.timestamp)}</span>
              </div>
            )}
          </Card>
          {isLive && <><CapsPanel status={status} decisions={feed} /><WalletPanel /><LiveGateCard /></>}
          <AgentIdentityCard />
          <Controls status={status} onChange={refresh} tick={tick} />
          <AskAgent tick={tick} />
          <AlertsPanel version={alertsVersion} />
        </div>
      </div>

      <div style={{ height: 16 }} />
      {isLive ? <ActivityFeed decisions={feed} confirmMode={!!status?.control.confirmation_mode} /> : <Card title={<>Decision log <span className="muted small" style={{ fontWeight: 400 }}>{shown.length}</span></>}
        extra={<Segmented size="sm" value={filter} onChange={setFilter} options={[{ value: "ALL", label: "All" }, { value: "TRADES", label: "Trades only" }]} />} pad={false}>
        <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
          <table>
            <thead><tr><th>Time</th><th>Action</th><th>Token</th><th>Regime</th><th>Strategy</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>
              {shown.map((d) => (
                <tr key={d.id}>
                  <td className="small muted">{time(d.timestamp)}</td><td><ActionBadge action={d.action} /></td><td>{d.symbol}</td>
                  <td><RegimeBadge regime={d.regime} /></td><td className="small trunc" title={d.strategy}>{strategyLabel(d.strategy)}</td>
                  <td className="small">{d.execution ? (d.execution.status === "PENDING_CONFIRMATION" && !status?.control.confirmation_mode ? "Waiting on chain" : humanize(d.execution.status)) : "—"} <TxLink exec={d.execution} /></td>
                  <td className="wrap small">{isExitQueued(d.reason) && <><span className="pill warn">Exit queued until fill</span>{" "}</>}{d.signal?.exit_reason && <><ExitPill d={d} />{" "}</>}{d.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!shown.length && <div className="empty">Nothing logged yet</div>}
        </div>
      </Card>}
      {closeTarget && (
        <ConfirmDialog title={closeTarget.kind === "lot" ? `Close ${closeTarget.symbol} lot ${closeTarget.id.slice(0, 6)}?` : `Close all ${closeTarget.symbol}?`}
          confirmLabel={closeTarget.kind === "lot" ? "Close lot" : `Close all ${closeTarget.symbol}`} danger busy={busy === "close"} onCancel={() => setCloseTarget(null)}
          onConfirm={async () => {
            setBusy("close");
            try {
              const d = closeTarget.kind === "lot" ? await api.closePositionById(closeTarget.id) : await api.closePosition(closeTarget.symbol);
              toast(`${closeTarget.symbol} ${closeTarget.kind === "lot" ? "lot" : "positions"} closed${d.execution?.avg_price ? ` @ ${usd(d.execution.avg_price)}` : ""}`, "success"); refresh();
            } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
            finally { setBusy(null); setCloseTarget(null); }
          }}>
          Manual override: sell {closeTarget.kind === "lot" ? "this lot" : `all ${lotCount(closeTarget.symbol)} open ${closeTarget.symbol} lots`} at the last price
          ({status?.control.trading_mode ?? "SIM"} mode), regardless of the strategy. Exit reason: MANUAL.
        </ConfirmDialog>
      )}
      {askReset && (
        <ConfirmDialog title="Reset emergency stop?" confirmLabel="Reset emergency stop" busy={busy === "reset"} onCancel={() => setAskReset(false)}
          onConfirm={async () => {
            setBusy("reset");
            try { await api.resetEmergencyStop(); toast("Emergency stop reset", "success"); refresh(); }
            catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
            finally { setBusy(null); setAskReset(false); }
          }}>
          New entries become possible again on the next cycle{status?.control.paused ? " once you also resume (the agent is still paused)" : ""}. Make sure the reason for the stop is resolved first.
        </ConfirmDialog>
      )}
    </>
  );
}

function DecisionDetail({ d }: { d: Decision }) {
  const v = d.risk_verdict;
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="kv">
        <span className="k">When</span><span className="small">{time(d.timestamp)} · {d.mode} · {d.agent_mode}</span>
        <span className="k">Token / strategy</span><span>{d.symbol} · <b>{strategyLabel(d.strategy)}</b></span>
        <span className="k">Regime</span><span><RegimeBadge regime={d.regime} /></span>
        <span className="k">Reason</span><span>{d.reason}</span>
        {d.signal?.exit_reason && <><span className="k">Exit</span><span><ExitPill d={d} />{d.signal.position_id && <span className="faint small"> · lot {d.signal.position_id.slice(0, 6)}</span>}</span></>}
        {v && <><span className="k">Risk check</span><span>{v.approved ? <span className="pill ok">approved</span> : <span className="pill bad">blocked</span>} <span className="small muted">{v.reasons?.join("; ")}{v.adjusted_notional ? ` ${usd(v.adjusted_notional)}` : ""}</span></span></>}
        {d.execution && <><span className="k">Execution</span><span className="small">{humanize(d.execution.status)} {d.execution.avg_price ? `@ ${usd(d.execution.avg_price)}` : ""} {d.execution.error ?? ""} <TxLink exec={d.execution} /></span></>}
      </div>
      {(d.signal?.conditions_met?.length || d.signal?.conditions_failed?.length) ? (
        <div className="chips">
          {(d.signal?.conditions_met ?? []).map((c) => <span key={"m" + c} className="chip pass" title={c}>✓ {conditionLabel(c)}</span>)}
          {(d.signal?.conditions_failed ?? []).map((c) => <span key={"f" + c} className="chip fail" title={c}>✕ {conditionLabel(c)}</span>)}
        </div>
      ) : null}
      {d.explanation && d.explanation !== d.reason && <div className="summary-box">{d.explanation}</div>}
    </div>
  );
}

/** Exit reason + intended fill price (Signal.price: SL/TP level or gap open) for SELL decisions. */
function ExitPill({ d }: { d: Decision }) {
  const r = d.signal?.exit_reason;
  if (!r) return null;
  const tone = r === "TAKE_PROFIT" ? "ok" : r === "STOP_LOSS" || r === "MAX_LOSS" || r === "EMERGENCY_STOP" ? "bad" : r === "TRAILING_STOP" ? "warn" : "";
  return <span className={`pill ${tone}`}>{EXIT_LABEL[r]}{d.signal?.price != null ? ` @ ${usd(d.signal.price)}` : ""}</span>;
}

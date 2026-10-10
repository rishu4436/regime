import { useMemo } from "react";
import type { BacktestResult, Candle } from "../contracts";
import { EquityChart, PriceChart, type TradeMarker } from "../components/Charts";
import { Card, Skeleton, StatCard } from "../components/ui";
import { cls, num, pct, time, usd } from "../lib/format";
import { describeEntry, EXIT_LABEL, type FullStrategy } from "../lib/strategy";

const EXIT_SHORT: Record<string, string> = { TRAILING_STOP: "TS", TAKE_PROFIT: "TP", STOP_LOSS: "SL", RSI_EXIT: "RSI", MACD_EXIT: "MACD", END: "End", MAX_LOSS: "ML", MANUAL: "M", EMERGENCY_STOP: "ES" };

export interface BacktestRun { result: BacktestResult; candles: Candle[]; strategy: FullStrategy; ranAt: string }

/** Stats, price chart with trade markers, equity curve, summary text and trade table. */
export function BacktestResults({ run, loading }: { run: BacktestRun | null; loading?: boolean }) {
  const markers = useMemo<TradeMarker[]>(() => (run?.result.trades ?? []).flatMap((t) => [
    { time: t.entry_time, type: "BUY" as const, text: "" },
    ...(t.exit_time ? [{ time: t.exit_time, type: "SELL" as const, text: t.exit_reason ? EXIT_SHORT[t.exit_reason] : "" }] : []),
  ]), [run]);

  if (!run) {
    return (
      <Card title="Results">
        {loading ? <div className="stack"><div className="stats">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} h={78} />)}</div><Skeleton h={320} /><Skeleton h={180} /></div>
          : <div className="empty">Configure your rules and press <b>Run backtest</b> to see return, win rate, drawdown, trades and charts.</div>}
      </Card>
    );
  }
  const r = run.result, s = run.strategy;
  const entry = describeEntry(s);
  const exits = [`TP ${s.exit.take_profit_pct}%`, `SL ${s.exit.stop_loss_pct}%`, s.exit.trailing_stop.enabled && `Trail ${s.exit.trailing_stop.pct}%`,
    s.exit.rsi_above.enabled && `RSI>${s.exit.rsi_above.threshold}`, s.exit.macd_bearish_cross.enabled && "MACD bearish"].filter(Boolean);
  const filters = [s.filters.market_hours.enabled && `market hours ${s.filters.market_hours.start_hour_utc}-${s.filters.market_hours.end_hour_utc} UTC`,
    s.filters.min_volume.enabled && `min volume ${s.filters.min_volume.multiplier}×avg`].filter(Boolean);
  const summary = `Token: ${s.symbol} · ${s.timeframe}
Entry (${s.entry.logic}): ${entry.join(", ") || "None"}
Exit: ${exits.join(" / ")}
Size: ${s.risk.position_size_pct}% | Max positions: ${s.risk.max_open_positions} | Cooldown: ${s.risk.cooldown_bars} bars${s.risk.max_loss_per_trade_pct != null ? ` | Max loss/trade ${s.risk.max_loss_per_trade_pct}%` : ""}
${filters.length ? `Filters: ${filters.join(", ")}\n` : ""}Result: ${pct(r.total_return_pct)} | ${r.num_trades} trades | Win rate ${r.win_rate_pct.toFixed(1)}% | Max DD ${r.max_drawdown_pct.toFixed(1)}%`;

  return (
    <div className="stack" style={{ opacity: loading ? 0.6 : 1, transition: "opacity .2s" }}>
      <div className="stats" style={{ marginBottom: 0, gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))" }}>
        <StatCard label="Return" value={r.total_return_pct} format={(v) => pct(v)} tone="auto" />
        <StatCard label="Win rate" value={r.win_rate_pct} format={(v) => `${v.toFixed(1)}%`} />
        <StatCard label="Trades" value={r.num_trades} format={(v) => Math.round(v).toString()} />
        <StatCard label="Max drawdown" value={r.max_drawdown_pct} format={(v) => `${v.toFixed(1)}%`} tone={r.max_drawdown_pct > 0 ? "neg" : undefined} />
        <StatCard label="Final capital" value={r.final_equity} format={(v) => usd(v, 0)} delta={r.total_return_pct} deltaLabel={`from ${usd(r.initial_capital, 0)}`} />
      </div>
      <Card title={<>Price & trades <span className="muted small" style={{ fontWeight: 400 }}>{r.symbol} · {run.candles.length} bars</span></>}
        extra={<div className="legend"><span><i style={{ background: "#22c55e" }} />Buy</span><span><i style={{ background: "#f87171" }} />Sell · TP/SL/TS(trail)/RSI/MACD</span></div>}>
        <PriceChart candles={run.candles} markers={markers} height={320} />
      </Card>
      <Card title="Equity curve" extra={<span className={`small ${cls(r.total_return_pct)}`}>{usd(r.final_equity)}</span>}>
        <EquityChart points={r.equity_curve ?? []} baseline={r.initial_capital} height={190} />
        <div className="summary-box" style={{ marginTop: 12 }}>{summary}</div>
      </Card>
      <Card title={<>Trades <span className="muted small" style={{ fontWeight: 400 }}>{r.trades?.length ?? 0}</span></>} pad={false}>
        <div className="table-wrap" style={{ padding: "4px 8px 8px" }}>
          <table>
            <thead><tr><th>#</th><th>Entry time</th><th className="r">Entry</th><th>Exit time</th><th className="r">Exit</th><th className="r">Qty</th><th className="r">PnL</th><th className="r">PnL %</th><th>Reason</th></tr></thead>
            <tbody>
              {(r.trades ?? []).map((t, i) => (
                <tr key={i}>
                  <td className="faint">{i + 1}</td><td className="small">{time(t.entry_time)}</td><td className="r">{usd(t.entry_price)}</td>
                  <td className="small">{time(t.exit_time)}</td><td className="r">{usd(t.exit_price)}</td><td className="r">{num(t.quantity)}</td>
                  <td className={`r ${cls(t.pnl)}`}>{usd(t.pnl)}</td><td className={`r ${cls(t.pnl_pct)}`}>{pct(t.pnl_pct)}</td>
                  <td>{t.exit_reason ? <span className={`pill ${t.exit_reason === "TAKE_PROFIT" ? "ok" : t.exit_reason === "STOP_LOSS" || t.exit_reason === "MAX_LOSS" ? "bad" : ""}`}>{EXIT_LABEL[t.exit_reason]}</span> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!r.trades?.length && <div className="empty">No trades: the entry conditions never fired on this data.</div>}
        </div>
      </Card>
      <div className="note">Backtest ran {new Date(run.ranAt).toLocaleTimeString()} · no fees or slippage in backtests (live/SIM execution includes them) ·{" "}
        results intentionally differ from the reference simulator: <a href="https://github.com/rishu4436/teamwork/blob/main/backend/engine/README.md" target="_blank" rel="noreferrer">why</a>.</div>
    </div>
  );
}

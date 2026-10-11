import { Card } from "../components/ui";
import { BacktestResults, type BacktestRun } from "./BacktestResults";

export const ENGINE_NOTES_URL = "https://github.com/rishu4436/regime/blob/main/backend/engine/README.md";

/** Full-width results of the latest backtest from the Manual Builder. */
export function BacktestView({ run, onOpenBuilder }: { run: BacktestRun | null; onOpenBuilder: () => void }) {
  return (
    <div className="stack">
      <div className="banner blue">
        <span className="grow">
          {run ? <>Showing <b>{run.strategy.name}</b> on {run.strategy.symbol}.</> : "No backtest yet."} Engine results deliberately differ from the
          reference simulator (Wilder RSI, intrabar SL/TP, rolling volume, real market hours); see <a href={ENGINE_NOTES_URL} target="_blank" rel="noreferrer">engine vs simulator notes</a>.
        </span>
        <button className="btn ghost" onClick={onOpenBuilder}>{run ? "Edit & re-run" : "Open builder"}</button>
      </div>
      {run ? <BacktestResults run={run} /> : <Card title="Results"><div className="empty">Run a backtest from the Manual Builder to see it here.</div></Card>}
    </div>
  );
}

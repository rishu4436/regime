import { useState } from "react";
import { CONTRACTS_VERSION, DEFAULT_INITIAL_CAPITAL, PresetNameValues, type AutoConfig } from "./contracts";
import { API_MODE } from "./api";
import { ModeBadge, RegimeBadge } from "./components/Badges";
import { ToastProvider } from "./components/ui";
import { useStatus } from "./lib/useStatus";
import { defaultStrategy, fillStrategy, type FullStrategy } from "./lib/strategy";
import { AutoMode } from "./views/AutoMode";
import type { BacktestRun } from "./views/BacktestResults";
import { BacktestView } from "./views/BacktestView";
import { Dashboard } from "./views/Dashboard";
import { ManualBuilder } from "./views/ManualBuilder";

const LS_KEY = "teamwork.strategy.v1";
function loadStrategy(): FullStrategy {
  try { const raw = localStorage.getItem(LS_KEY); return raw ? fillStrategy(JSON.parse(raw)) : defaultStrategy(); } catch { return defaultStrategy(); }
}

export type Tab = "dashboard" | "manual" | "auto" | "backtest";
const TABS: { id: Tab; label: string; ico: string; title: string; sub: string }[] = [
  { id: "dashboard", label: "Dashboard", ico: "◧", title: "Dashboard", sub: "Live agent state, positions and decisions" },
  { id: "manual", label: "Manual Builder", ico: "⚙", title: "Manual Strategy Builder", sub: "Define entry, exit, risk and filters, then backtest and activate" },
  { id: "auto", label: "Auto Mode", ico: "✦", title: "Auto Mode", sub: "Pick a trader style; the agent maps the market regime to one preset" },
  { id: "backtest", label: "Backtest", ico: "◔", title: "Backtest results", sub: "Stats, price chart with trades, equity curve and trade list" },
];

export default function App() {
  return <ToastProvider><Shell /></ToastProvider>;
}

function Shell() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const { status, error, refresh, alertsVersion, tick } = useStatus();
  const pendingCount = (status?.recent_decisions ?? []).filter((d) => d.execution?.status === "PENDING_CONFIRMATION").length;
  const meta = TABS.find((t) => t.id === tab) ?? TABS[0];
  const go = (t: Tab) => setTab(t);
  const [strategy, setStrategyState] = useState<FullStrategy>(loadStrategy);
  const setStrategy = (st: FullStrategy) => { setStrategyState(st); try { localStorage.setItem(LS_KEY, JSON.stringify(st)); } catch { /* private mode */ } };
  const [capital, setCapital] = useState(DEFAULT_INITIAL_CAPITAL);
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [autoCfg, setAutoCfg] = useState<AutoConfig>({ symbol: "NVDAon", style: "BALANCED", enabled_presets: [...PresetNameValues], capital: DEFAULT_INITIAL_CAPITAL, timeframe: "1h", activation: "CONFIRMATION" });

  const nav = TABS.map((t) => (
    <button key={t.id} className={`nav-item${tab === t.id ? " active" : ""}`} onClick={() => setTab(t.id)}>
      <span className="ico">{t.ico}</span>{t.label}
      {t.id === "dashboard" && pendingCount > 0 && <span className="count">{pendingCount}</span>}
    </button>
  ));

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand"><div className="brand-logo">◆</div><div><b>Teamwork Agent</b><small>On-chain stocks</small></div></div>
        {nav}
        <div className="sidebar-foot">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>Mode <ModeBadge mode={status?.control.trading_mode} /></div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>API <span className="pill">{API_MODE === "mock" ? "mock" : "backend"}</span></div>
          <div className="faint">contracts v{CONTRACTS_VERSION}</div>
        </div>
      </aside>
      <main className="main">
        <div className="topbar">
          <div className="grow"><h1>{meta.title}</h1><div className="sub">{meta.sub}</div></div>
          {status?.current_regime && <RegimeBadge regime={status.current_regime} />}
          {status && (status.control.emergency_stopped ? <span className="pill bad"><span className="dot" />Stopped</span>
            : status.control.paused ? <span className="pill warn"><span className="dot" />Paused</span>
            : <span className="pill ok"><span className="live-dot" />Live loop</span>)}
          <ModeBadge mode={status?.control.trading_mode} />
        </div>
        {error && <div className="banner amber"><span className="grow">Backend unreachable: {error}</span><span className="small muted">Start the API or run with VITE_API_MODE=mock</span></div>}
        {tab === "dashboard" && <Dashboard status={status} refresh={refresh} tick={tick} alertsVersion={alertsVersion} go={go} />}
        {tab === "manual" && <ManualBuilder strategy={strategy} setStrategy={setStrategy} capital={capital} setCapital={setCapital} run={run} setRun={setRun}
          onActivated={() => { refresh(); setTab("dashboard"); }} onAutoParsed={(c) => { setAutoCfg({ ...autoCfg, ...c }); setTab("auto"); }} />}
        {tab === "auto" && <AutoMode config={autoCfg} setConfig={setAutoCfg} status={status} tick={tick} onActivated={() => { refresh(); setTab("dashboard"); }} />}
        {tab === "backtest" && <BacktestView run={run} onOpenBuilder={() => setTab("manual")} />}
      </main>
      <nav className="mobile-nav">{nav}</nav>
    </div>
  );
}

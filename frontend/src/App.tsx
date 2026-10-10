import { useEffect, useState } from "react";
import { CONTRACTS_VERSION } from "./contracts";
import { API_MODE } from "./api";
import { ModeBadge, RegimeBadge } from "./components/Badges";
import { ToastProvider } from "./components/ui";
import { useStatus } from "./lib/useStatus";
import { Dashboard } from "./views/Dashboard";

export type Tab = "dashboard" | "manual" | "auto" | "backtest";
const TABS: { id: Tab; label: string; ico: string; title: string; sub: string }[] = [
  { id: "dashboard", label: "Dashboard", ico: "◧", title: "Dashboard", sub: "Live agent state, positions and decisions" },
];

export default function App() {
  return <ToastProvider><Shell /></ToastProvider>;
}

function Shell() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const { status, error, refresh, lastEvent } = useStatus();
  const [tick, setTick] = useState(0);
  useEffect(() => { if (lastEvent?.type === "PRICE") setTick((t) => t + 1); }, [lastEvent]);
  const pendingCount = (status?.recent_decisions ?? []).filter((d) => d.execution?.status === "PENDING_CONFIRMATION").length;
  const meta = TABS.find((t) => t.id === tab) ?? TABS[0];
  const go = (t: Tab) => setTab(TABS.some((x) => x.id === t) ? t : "dashboard");

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
        {tab === "dashboard" && <Dashboard status={status} refresh={refresh} tick={tick} go={go} />}
      </main>
      <nav className="mobile-nav">{nav}</nav>
    </div>
  );
}

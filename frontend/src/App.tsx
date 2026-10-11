import { useEffect, useState } from "react";
import { CONTRACTS_VERSION, DEFAULT_INITIAL_CAPITAL, PresetNameValues, type AutoConfig } from "./contracts";
import { API_MODE } from "./api";
import { MOCK_SCENARIO, MOCK_WARNINGS, setMockScenario, setMockWarnings } from "./api/mock";
import { LiveBanner, SessionBanner } from "./components/Live";
import { LiveContext, useLiveState } from "./lib/live";
import { WalletButton, WalletChangedBanner, WalletConnectProvider } from "./components/WalletConnect";
import { ModeBadge, RegimeBadge } from "./components/Badges";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { StatusBar } from "./components/StatusBar";
import { Toggle, ToastProvider } from "./components/ui";
import { useStatus } from "./lib/useStatus";
import type { WatchlistFields } from "./api/types";
import { defaultStrategy, fillStrategy, type FullStrategy } from "./lib/strategy";
import { AutoMode } from "./views/AutoMode";
import type { BacktestRun } from "./views/BacktestResults";
import { BacktestView } from "./views/BacktestView";
import { Dashboard } from "./views/Dashboard";
import { ManualBuilder } from "./views/ManualBuilder";
import { AlertsView, AskView, IdentityView, LimitsView, WalletView } from "./views/Tabs";

// v2: activation defaults to AUTO_EXECUTE everywhere; older saved drafts (CONFIRMATION default) are dropped
const LS_KEY = "teamwork.strategy.v2";
function loadStrategy(): FullStrategy {
  try { const raw = localStorage.getItem(LS_KEY); return raw ? fillStrategy(JSON.parse(raw)) : defaultStrategy(); } catch { return defaultStrategy(); }
}

export type Tab = "dashboard" | "manual" | "auto" | "backtest" | "wallet" | "limits" | "identity" | "ask" | "alerts";
const TABS: { id: Tab; label: string; ico: string; title: string; sub: string; group?: string }[] = [
  { id: "dashboard", label: "Dashboard", ico: "◧", title: "Dashboard", sub: "Agent status and controls, positions, chart and decisions", group: "Trade" },
  { id: "manual", label: "Manual Builder", ico: "⚙", title: "Manual Strategy Builder", sub: "Define entry, exit, risk and filters, then backtest and activate" },
  { id: "auto", label: "Auto Mode", ico: "✦", title: "Auto Mode", sub: "Pick a trader style; the agent maps the market regime to one preset" },
  { id: "backtest", label: "Backtest", ico: "◔", title: "Backtest results", sub: "Stats, price chart with trades, equity curve and trade list" },
  { id: "wallet", label: "Wallet", ico: "◈", title: "Wallet", sub: "Agentic Wallet connection, balances and gas", group: "Agent" },
  { id: "limits", label: "Trading limits", ico: "⛨", title: "Trading limits", sub: "Spending caps, decision loop and confirmation mode" },
  { id: "identity", label: "Agent identity", ico: "⬢", title: "Agent identity", sub: "BNB Agent Studio (ERC-8004) identity and paid second opinions (x402)" },
  { id: "ask", label: "Ask the agent", ico: "✉", title: "Ask the agent", sub: "Ask why it did something, in plain language" },
  { id: "alerts", label: "Alerts", ico: "⚑", title: "Alerts", sub: "Stops, pauses, risk blocks and losing streaks" },
];

export default function App() {
  return <ToastProvider><Shell /></ToastProvider>;
}

function Shell() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const { status, error, refresh, alertsVersion, tick } = useStatus();
  const live = useLiveState(status, tick);
  const mode = live.isLive ? "LIVE" : status?.control.trading_mode ?? live.system?.trading_mode;
  // the agent is autonomous in every mode: activations default to AUTO_EXECUTE; confirmation is an explicit opt-in toggle
  const pendingCount = (status?.recent_decisions ?? []).filter((d) => d.execution?.status === "PENDING_CONFIRMATION").length;
  const meta = TABS.find((t) => t.id === tab) ?? TABS[0];
  const go = (t: Tab) => setTab(t);
  const [strategy, setStrategyState] = useState<FullStrategy>(loadStrategy);
  const setStrategy = (st: FullStrategy) => { setStrategyState(st); try { localStorage.setItem(LS_KEY, JSON.stringify(st)); } catch { /* private mode */ } };
  const [capital, setCapital] = useState(DEFAULT_INITIAL_CAPITAL);
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [autoCfg, setAutoCfg] = useState<AutoConfig & WatchlistFields>({ symbol: "NVDAB", style: "BALANCED", enabled_presets: [...PresetNameValues], capital: DEFAULT_INITIAL_CAPITAL, timeframe: "1h", activation: "AUTO_EXECUTE" });

  // unread = alerts pushed over the socket since the Alerts tab was last open
  const [alertsSeen, setAlertsSeen] = useState(0);
  useEffect(() => { if (tab === "alerts") setAlertsSeen(alertsVersion); }, [tab, alertsVersion]);
  const unread = tab === "alerts" ? 0 : Math.max(0, alertsVersion - alertsSeen);
  const navItems = (withGroups: boolean) => TABS.flatMap((t) => [
    ...(withGroups && t.group ? [<div key={`g-${t.group}`} className="nav-group">{t.group}</div>] : []),
    <button key={t.id} className={`nav-item${tab === t.id ? " active" : ""}`} onClick={() => setTab(t.id)} title={t.title}>
      <span className="ico">{t.ico}</span><span className="nav-label">{t.label}</span>
      {t.id === "dashboard" && pendingCount > 0 && <span className="count">{pendingCount}</span>}
      {t.id === "alerts" && unread > 0 && <span className="count" title={`${unread} new alert${unread > 1 ? "s" : ""}`}>{unread > 99 ? "99+" : unread}</span>}
    </button>,
  ]);

  return (
    <LiveContext.Provider value={live}>
    <WalletConnectProvider>
    <div className={`app${live.isLive ? " is-live" : ""}`}>
      <aside className="sidebar">
        <div className="brand"><div className="brand-logo">◆</div><div><b>Regime</b><small>Autonomous tokenized-stock agent</small></div></div>
        <nav className="side-nav">{navItems(true)}</nav>
        <div className="sidebar-foot">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>Mode <ModeBadge mode={mode} /></div>
          {live.isLive && <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>Network <span className="pill bad">BSC mainnet</span></div>}
          {API_MODE === "mock" && (
            <div className="scenario" title="Mock only: pretend the backend runs LIVE on mainnet with an Agentic Wallet. Nothing is sent anywhere.">
              <span className="small">Demo LIVE scenario</span>
              <Toggle checked={MOCK_SCENARIO === "LIVE"} onChange={(v) => setMockScenario(v ? "LIVE" : "SIM")} label="Demo LIVE scenario" />
            </div>
          )}
          {API_MODE === "mock" && MOCK_SCENARIO === "LIVE" && (
            <div className="scenario" title="Mock only: show every risk warning and an expired wallet session. Nothing is sent anywhere.">
              <span className="small">Demo warnings</span>
              <Toggle checked={MOCK_WARNINGS} onChange={setMockWarnings} label="Demo warnings" />
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>API <span className="pill">{API_MODE === "mock" ? "mock" : "backend"}</span></div>
          <div className="faint">contracts v{CONTRACTS_VERSION}</div>
        </div>
      </aside>
      <main className="main">
        <div className="topbar">
          <div className="grow"><h1>{meta.title}</h1><div className="sub">{meta.sub}</div></div>
          {status?.current_regime && <RegimeBadge regime={status.current_regime} />}
          {live.isLive && <span className="live-tag" title="Real funds on BNB Smart Chain mainnet"><span className="live-dot" />LIVE · MAINNET</span>}
          <WalletButton addressOnly />
        </div>
        {API_MODE === "mock" && <div className="banner amber" style={{ marginBottom: 12 }}><span className="grow small">
          <b>Mock data:</b> this dashboard isn't connected to the Regime backend, so nothing here is real. Start it with <code>VITE_API_MODE=http</code> to see the live agent.</span></div>}
        <ErrorBoundary label="status banners">
          <LiveBanner />
          <SessionBanner />
          <WalletChangedBanner />
        </ErrorBoundary>
        <ErrorBoundary label="status bar"><StatusBar status={status} refresh={refresh} /></ErrorBoundary>
        {error && <div className="banner amber"><span className="grow">Backend unreachable: {error}</span><span className="small muted">Start the API or run with VITE_API_MODE=mock</span></div>}
        <ErrorBoundary key={tab} label={meta.title}>
        {tab === "dashboard" && <Dashboard status={status} refresh={refresh} tick={tick} go={go} />}
        {tab === "manual" && <ManualBuilder strategy={strategy} setStrategy={setStrategy} capital={capital} setCapital={setCapital} run={run} setRun={setRun}
          onActivated={() => { refresh(); setTab("dashboard"); }} onAutoParsed={(c) => { setAutoCfg({ ...autoCfg, ...c, auto_pick: autoCfg.auto_pick, activation: autoCfg.activation }); setTab("auto"); }} />}
        {tab === "auto" && <AutoMode config={autoCfg} setConfig={setAutoCfg} status={status} tick={tick} onActivated={() => { refresh(); setTab("dashboard"); }} />}
        {tab === "backtest" && <BacktestView run={run} onOpenBuilder={() => setTab("manual")} />}
        {tab === "wallet" && <WalletView />}
        {tab === "limits" && <LimitsView status={status} refresh={refresh} tick={tick} />}
        {tab === "identity" && <IdentityView status={status} refresh={refresh} tick={tick} />}
        {tab === "ask" && <AskView tick={tick} />}
        {tab === "alerts" && <AlertsView version={alertsVersion} />}
        </ErrorBoundary>
      </main>
      <nav className="mobile-nav">{navItems(false)}</nav>
    </div>
    </WalletConnectProvider>
    </LiveContext.Provider>
  );
}

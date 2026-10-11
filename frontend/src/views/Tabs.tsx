// Pages that used to sit in the long Dashboard right column, each now its own tab.
import { useEffect, useState } from "react";
import type { Decision, StatusSnapshot } from "../contracts";
import { api } from "../api";
import { AgentIdentityCard, CapsPanel, WalletPanel } from "../components/Live";
import { StockAgentCard } from "../components/StockAgent";
import { TradingLimitsCard } from "../components/TradingLimits";
import { WalletButton } from "../components/WalletConnect";
import { Card } from "../components/ui";
import { useLive } from "../lib/live";
import { Controls } from "./Controls";
import { AlertsPanel, AskAgent } from "./Insights";

interface Common { status: StatusSnapshot | null; refresh: () => void; tick: number }

/** Wallet: balances/gas (WalletPanel) and how to connect or re-sign the Agentic Wallet. */
export function WalletView() {
  const { isLive, wallet, system } = useLive();
  const expired = !!wallet?.session_expired;
  return (
    <div className="split">
      <WalletPanel />
      <Card title="Connect" extra={isLive ? <WalletButton /> : null}>
        {isLive ? (
          <div className="small muted stack" style={{ gap: 8 }}>
            {wallet?.connected && !expired
              ? <div><b style={{ color: "var(--text)" }}>Binance Agentic Wallet is connected.</b> The agent trades from it on its own, inside your trading limits.</div>
              : <div><b style={{ color: "var(--text)" }}>{expired ? "The wallet sign-in expired." : "No wallet connected."}</b> Click <b>{expired ? "Reconnect wallet" : "Connect wallet"}</b> and scan the QR code with the Binance App.</div>}
            <div>Keep some BNB in the wallet for gas. If you switch to a different wallet, a banner at the top asks you to check positions before trading resumes.</div>
          </div>
        ) : (
          <div className="small muted">The agent is in <b>{system?.trading_mode ?? "SIM"}</b> mode, so it trades with simulated cash and no wallet is needed. A wallet is only connected when the backend runs LIVE on BNB Smart Chain.</div>
        )}
      </Card>
    </div>
  );
}

/** Trading limits (editable caps), live caps usage, and the decision loop / confirmation-mode settings. */
export function LimitsView({ status, refresh, tick }: Common) {
  const { isLive } = useLive();
  const [feed, setFeed] = useState<Decision[]>([]);
  useEffect(() => { if (isLive) api.getDecisions(60).then(setFeed).catch(() => {}); }, [isLive, status, tick]);
  return (
    <div className="split">
      <div className="stack">
        <TradingLimitsCard refresh={refresh} />
        {isLive && <CapsPanel status={status} decisions={feed} />}
      </div>
      <div className="stack"><Controls status={status} onChange={refresh} tick={tick} /></div>
    </div>
  );
}

/** Agent identity: Agent Studio (ERC-8004) id + register on-chain, and the x402 second opinion for the active token. */
export function IdentityView({ status, tick }: Common) {
  const symbol = status?.active_symbol ?? "NVDAB";
  return (
    <div className="grid cols-2" style={{ alignItems: "start" }}>
      <AgentIdentityCard />
      <StockAgentCard symbol={symbol} tick={tick} />
    </div>
  );
}

export function AskView({ tick }: { tick: number }) {
  return <div style={{ maxWidth: 860 }}><AskAgent tick={tick} full /></div>;
}

export function AlertsView({ version }: { version: number }) {
  return <div style={{ maxWidth: 960 }}><AlertsPanel version={version} limit={100} full /></div>;
}

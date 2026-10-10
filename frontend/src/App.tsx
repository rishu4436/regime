import { useEffect, useState } from "react";
import { CONTRACTS_VERSION, RegimeValues, type ControlState, type TokenInfo } from "./contracts";

// Placeholder shell. Bot 1 replaces this with the Manual Builder, Auto Mode and dashboard.
export default function App() {
  const [control, setControl] = useState<ControlState | null>(null);
  const [tokens, setTokens] = useState<TokenInfo[]>([]);

  useEffect(() => {
    fetch("/api/control").then((r) => r.json()).then(setControl).catch(() => setControl(null));
    fetch("/api/tokens").then((r) => r.json()).then(setTokens).catch(() => setTokens([]));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui", padding: 24 }}>
      <h1>Autonomous On-Chain Stock Trading Agent</h1>
      <p>Contracts v{CONTRACTS_VERSION} · mode: {control?.trading_mode ?? "backend offline"}</p>
      <p>Regimes: {RegimeValues.join(", ")}</p>
      <ul>
        {tokens.map((t) => (
          <li key={t.symbol}>
            {t.symbol} — {t.name} ({t.chain}) {t.price != null ? `$${t.price.toFixed(2)}` : ""}
          </li>
        ))}
      </ul>
    </main>
  );
}

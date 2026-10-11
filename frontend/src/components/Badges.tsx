import type { Action, Regime, TradingMode } from "../contracts";
import { REGIME_INFO } from "../lib/auto";

export const ModeBadge = ({ mode }: { mode?: TradingMode | null }) => (
  <span className={`pill ${mode ?? "SIM"}`} title="Trading mode: SIM = mocks, PAPER = real data + simulated fills, LIVE = real funds">
    <span className="dot" />{mode ?? "SIM"}
  </span>
);
export const ActionBadge = ({ action }: { action: Action }) => <span className={`pill ${action}`}>{action === "STAY_OUT" ? "STAY OUT" : action}</span>;
export const RegimeBadge = ({ regime }: { regime?: Regime | null }) =>
  regime ? (
    <span className="pill" style={{ color: REGIME_INFO[regime].color, background: REGIME_INFO[regime].color + "1f", borderColor: REGIME_INFO[regime].color + "55" }}>
      <span className="dot" />{REGIME_INFO[regime].label}
    </span>
  ) : <span className="pill STAY_OUT">—</span>;

// LIVE / 24-7 autonomous monitor: mainnet banner, autonomy + heartbeat, kill switches, caps usage, wallet, live gate,
// activity feed with BscScan links.
import { useEffect, useState } from "react";
import type { Decision, ExecutionResult, StatusSnapshot } from "../contracts";
import { api } from "../api";
import type { ChainPending, LiveCooldown, LiveExecutionFields } from "../api/types";
import { cls, time, usd } from "../lib/format";
import { ago, allowedTokensText, balAmount, balAsset, balValue, bscAddr, bscTx, dur, limitsOf, shortHex, stricter, useLive } from "../lib/live";
import { conditionLabel, controlReason, exchangeCodeLabels, isExitQueued, strategyLabel } from "../lib/strategy";
import { ActionBadge } from "./Badges";
import { ConfirmDialog } from "./ConfirmDialog";
import { Card, Skeleton, useToast } from "./ui";

const NA = <span className="faint">not available</span>;
/** token_status on /api/tokens items (requested from Bot 6); hidden when absent. */
export interface TokenStatus { trading_status?: "TRADING" | "OFFHOURS" | "CLOSED" | "NO_LIQUIDITY" | "UNKNOWN" | string; tradable?: boolean | null; min_order_usdt?: number | null; next_open_time?: string | null }
let tokenCache: Promise<Record<string, TokenStatus | undefined>> | null = null;
export function TokenStatusBadge({ symbol }: { symbol: string }) {
  const [st, setSt] = useState<TokenStatus | undefined>();
  useEffect(() => {
    tokenCache ??= api.getTokens().then((ts) => Object.fromEntries(ts.map((t) => [t.symbol, (t as typeof t & { token_status?: TokenStatus }).token_status])));
    let live = true; tokenCache.then((m) => live && setSt(m[symbol])).catch(() => {}); return () => { live = false; };
  }, [symbol]);
  if (!st?.trading_status) return null;
  const s = st.trading_status, cls2 = s === "TRADING" ? "ok" : s === "OFFHOURS" ? "warn" : "";
  const label = s === "TRADING" ? "Trading" : s === "OFFHOURS" ? "Off-hours" : s === "CLOSED" ? "Closed" : s === "NO_LIQUIDITY" ? "No liquidity" : "Unknown";
  return (
    <span className={`pill ${cls2}`} style={{ marginLeft: 6, fontSize: ".68rem" }}
      title={[st.next_open_time ? `Opens ${new Date(st.next_open_time).toLocaleString()}` : "", st.min_order_usdt ? `Min order ${st.min_order_usdt} USDT` : ""].filter(Boolean).join(" · ") || undefined}>
      <span className="dot" />{label}{s === "OFFHOURS" && st.next_open_time ? ` · opens ${new Date(st.next_open_time).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : ""}{st.min_order_usdt ? ` · min ${st.min_order_usdt} USDT` : ""}
    </span>
  );
}

/** Token family from the symbol suffix: …B = bStock, …on = Ondo, …x = xStock. */
export const tokenFamily = (sym: string) => (/on$/.test(sym) ? "Ondo" : /x$/.test(sym) ? "xStock" : /B$/.test(sym) ? "bStock" : null);
export const FamilyBadge = ({ symbol }: { symbol: string }) => { const f = tokenFamily(symbol); return f ? <span className={`pill fam-${f}`} title={`${f} token`}>{f}</span> : null; };
const AUTO_STOP = /daily loss|automatic|auto-stop|stopped automatically/i;

/** Red, non-dismissible LIVE / MAINNET banner shown (sticky) on every tab whenever the backend runs LIVE. */
const SESSION = /session|qr|re-?sign|sign ?in|re-?auth|expired/i;
const SESSION_CODES = /\b(session_expired|wallet_not_ready)\b/i;
/** Agentic Wallet session expired (wallet.session_expired or a session/QR blocked reason): re-sign-in needed. */
export function SessionBanner() {
  const { wallet, gate } = useLive();
  const why = gate?.blocked_reasons.find((r) => SESSION_CODES.test(r) || (SESSION.test(r) && /wallet|session|qr|baw/i.test(r)));
  if (!wallet?.session_expired && !why) return null;
  return (
    <div className="banner red" role="alert">
      <span className="grow"><b>Agentic Wallet session expired, re-scan QR on host PC.</b> The agent can't trade until the wallet is signed in again (<code>baw auth signin</code>, then scan the QR in the Binance App).{why ? <><br /><span className="small">{why}</span></> : null}</span>
    </div>
  );
}

/** Agent identity: BNB Agent Studio (ERC-8004) id from /api/system.agent_identity, and the execution wallet. */
export function AgentIdentityCard() {
  const { system, wallet } = useLive();
  if (!system) return <Card title="Agent identity"><Skeleton h={60} /></Card>;
  const id = (system as typeof system & { agent_identity?: { erc8004_id?: string | number | null; registry?: string | null; studio_version?: string | null; network?: string | null; explorer_url?: string | null } | null }).agent_identity;
  return (
    <Card title="Agent identity" extra={<span className="pill">BNB Agent Studio · ERC-8004</span>}>
      {!id && <div className="small muted" style={{ marginBottom: 8 }}>Agent Studio identity: <b>not registered yet</b></div>}
      <div className="kv">
        <span className="k">ERC-8004 id</span><span className="mono">{id?.erc8004_id != null
          ? (id.explorer_url ? <a href={id.explorer_url} target="_blank" rel="noreferrer" title="View on 8004scan">{String(id.erc8004_id)} ↗</a> : String(id.erc8004_id))
          : (id ? <span className="faint">placeholder</span> : NA)}</span>
        <span className="k">Network</span><span className="small">{id?.network ?? NA}</span>
        <span className="k">Registry</span><span className="small">{id?.registry ?? NA}</span>
        <span className="k">Studio</span><span className="small">{id?.studio_version ?? NA}</span>
        <span className="k">Executes with</span><span><span className="pill">Agentic Wallet (baw)</span>{wallet?.address ? <a className="mono small" style={{ marginLeft: 6 }} href={bscAddr(wallet.address)} target="_blank" rel="noreferrer">{shortHex(wallet.address)}</a> : null}</span>
      </div>
      {api.kind === "mock" && <div className="note">Placeholder identity in mock mode.</div>}
    </Card>
  );
}

export function LiveBanner() {
  const { isLive, wallet, gate } = useLive();
  if (!isLive) return null;
  const L = limitsOf(wallet);
  return (
    <div className="banner live-banner sticky" role="alert">
      <span className="live-tag"><span className="live-dot" />LIVE · MAINNET</span>
      <span className="grow">
        <b>Real funds, autonomous 24/7.</b> The agent trades on <b>BNB Smart Chain mainnet</b> from{" "}
        {wallet?.address ? <a href={bscAddr(wallet.address)} target="_blank" rel="noreferrer" className="mono">{shortHex(wallet.address)}</a> : "the connected wallet"}
        {wallet?.provider === "baw" ? " (Binance Agentic Wallet)" : ""} without asking, inside hard caps
        {L.per_order_cap_usdt != null ? <> ({usd(stricter(L.per_order_cap_usdt, L.wallet?.per_order_cap_usdt), 0)}/order, {usd(L.daily_loss_cap_usdt, 0)} daily loss stop, {usd(L.max_exposure_usdt, 0)} open)</> : null}.
        {gate && !gate.can_trade && gate.blocked_reasons.length > 0 && <><br /><b>New trades blocked:</b> {gate.blocked_reasons.join(" · ")}</>}
      </span>
      {api.kind === "mock" && <span className="pill warn" title="Mock LIVE scenario: nothing is sent anywhere">demo · no funds</span>}
    </div>
  );
}

/** "tx 0x12ab…cdef ↗" for executions that carry a tx hash (explorer_url from the backend, else BscScan). */
export function TxLink({ exec }: { exec?: (ExecutionResult & LiveExecutionFields) | null }) {
  if (!exec?.tx_hash) return null;
  if (/^0xsim/i.test(exec.tx_hash)) return <span className="tx-link faint" title={exec.tx_hash}>simulated</span>; // SIM/PAPER hashes: never linked
  const demo = api.kind === "mock";
  return (
    <a className="tx-link mono" href={exec.explorer_url || bscTx(exec.tx_hash)} target="_blank" rel="noreferrer"
      title={demo ? "Demo hash from the mock LIVE scenario: not on chain" : `View ${exec.tx_hash} on BscScan`} onClick={(e) => e.stopPropagation()}>
      tx {shortHex(exec.tx_hash, 4)} ↗{demo ? " (demo)" : ""}
    </a>
  );
}

/** 24/7 monitor: is the agent running on its own, since when, heartbeat, and the kill switches. */
export function AutonomyMonitor({ status, refresh }: { status: StatusSnapshot | null; refresh: () => void }) {
  const toast = useToast();
  const { loop, hb, now, isLive, gate, refresh: refreshLive } = useLive();
  const [ask, setAsk] = useState<null | "stop" | "reset">(null);
  const [busy, setBusy] = useState(false);
  if (!status || !loop) return <Card><Skeleton h={70} /></Card>;
  const c = status.control;
  const auto = c.emergency_stopped && AUTO_STOP.test(c.reason ?? "");
  const [label, tone, icon] = c.emergency_stopped ? [auto ? "Auto-stopped: daily loss cap hit" : "Emergency stopped", "bad", "■"]
    : c.paused ? ["Paused by kill switch", "warn", "❚❚"]
    : !status.agent_mode ? ["Idle: no agent active", "", "○"]
    : !loop.running ? ["Agent active, loop stopped", "warn", "○"]
    : hb.stale ? ["Heartbeat lost: loop not ticking", "bad", "!"]
    : c.confirmation_mode ? ["Running (confirmation mode on)", "ok", "●"]
    : ["Agent running autonomously", "ok", "●"];
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    try { await fn(); toast(msg, "success"); refresh(); refreshLive(); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    finally { setBusy(false); setAsk(null); }
  };
  const nextIn = hb.nextTick != null ? Math.max(0, Math.round((hb.nextTick - now) / 1000)) : null;
  return (
    <section className={`card autonomy ${tone}`}>
      <div className="autonomy-main">
        <div className={`beat ${tone}${hb.stale ? " stale" : ""}`} aria-hidden>{icon}</div>
        <div className="grow">
          <div className="autonomy-title">{label}</div>
          <div className="small muted">
            {status.agent_mode ? <>{status.agent_mode} · {strategyLabel(status.active_strategy)} · {status.active_symbol}</> : "Activate a strategy in Manual Builder or Auto Mode"}
            {c.emergency_stopped || c.paused ? <> · {controlReason(c.reason) || "no reason given"}</> : null}
          </div>
        </div>
        <div className="btn-row">
          {c.paused
            ? <button className="btn green" disabled={busy} onClick={() => run(() => api.resume(), "Agent resumed")}>▶ Resume</button>
            : <button className="btn amber" disabled={busy || c.emergency_stopped} onClick={() => run(() => api.pause("Paused by user (kill switch)"), "Agent paused")}>❚❚ Pause</button>}
          {c.emergency_stopped
            ? <button className="btn ghost" disabled={busy} onClick={() => setAsk("reset")}>↺ Reset e-stop</button>
            : <button className="btn red" disabled={busy} onClick={() => setAsk("stop")}>■ Emergency stop</button>}
        </div>
      </div>
      <div className="autonomy-stats">
        <div><span className="lbl">Running since</span><b>{loop.running ? dur(hb.runningSince, now) : "stopped"}</b>{hb.runningSince && loop.running ? <span className="faint small">{hb.observed ? "seen by this page" : new Date(hb.runningSince).toLocaleString()}{loop.heartbeat?.autostart ? " · autostart" : ""}</span> : null}</div>
        <div><span className="lbl">Last tick</span><b className={hb.stale ? "neg" : ""}>{ago(hb.lastTick, now)}</b><span className="faint small">{loop.ticks} ticks{loop.heartbeat?.restarts ? ` · ${loop.heartbeat.restarts} restart${loop.heartbeat.restarts > 1 ? "s" : ""}` : ""}</span></div>
        <div><span className="lbl">Next tick</span><b>{loop.running ? (nextIn == null ? "—" : nextIn === 0 ? "now" : `in ${nextIn}s`) : "—"}</b><span className="faint small">every {loop.interval_seconds}s</span></div>
        <div><span className="lbl">Execution</span><b>{c.confirmation_mode ? "asks before entries" : "autonomous"}</b><span className="faint small">{isLive ? "real funds" : c.trading_mode}</span></div>
      </div>
      <InFlight cooldowns={gate?.cooldowns} pending={gate?.chain_pending} />
      {loop.heartbeat?.last_error && <div className="banner amber" style={{ margin: "12px 0 0" }}><span className="grow small"><b>Last loop error:</b> {loop.heartbeat.last_error}</span></div>}
      {hb.stale && <div className="banner red" style={{ margin: "12px 0 0" }}><span className="grow"><b>No tick for {ago(hb.lastTick, now).replace(" ago", "")}.</b> The loop says it is running but hasn't produced a cycle; check the backend logs.</span></div>}
      {auto && <div className="banner red" style={{ margin: "12px 0 0" }}><span className="grow"><b>Automatic emergency stop:</b> {controlReason(c.reason)}. No new entries until you reset it.</span></div>}
      {ask === "stop" && (
        <ConfirmDialog title="Emergency stop?" danger confirmLabel="Stop the agent" busy={busy} onCancel={() => setAsk(null)}
          onConfirm={() => run(() => api.emergencyStop("Emergency stop by user", false), "Emergency stop engaged")}>
          The agent stops opening trades immediately. Open positions are closed on the next bar at that bar's close (exit reason EMERGENCY_STOP).
        </ConfirmDialog>
      )}
      {ask === "reset" && (
        <ConfirmDialog title="Reset the emergency stop?" confirmLabel="Reset and resume trading" busy={busy} onCancel={() => setAsk(null)}
          onConfirm={() => run(() => api.resetEmergencyStop(), "Emergency stop reset")}>
          {auto ? "It was triggered by the daily loss cap. " : ""}The agent goes back to trading on its own{isLive ? " with real funds" : ""}.
        </ConfirmDialog>
      )}
    </section>
  );
}

const Meter = ({ label, used, cap, hint, warnAt = 0.8, children }: { label: React.ReactNode; used?: number | null; cap?: number | null; hint?: React.ReactNode; warnAt?: number; children?: React.ReactNode }) => {
  const r = used != null && cap ? Math.min(1, used / cap) : null;
  return (
    <div className="meter">
      <div className="meter-head"><span>{label}</span><span>{cap == null ? NA : <><b>{used != null ? usd(used) : "—"}</b> <span className="faint">/ {usd(cap)}</span></>}</span></div>
      <div className="meter-bar"><span style={{ width: `${(r ?? 0) * 100}%` }} className={r == null ? "" : r >= 1 ? "full" : r >= warnAt ? "warn" : ""} /></div>
      {(hint || children) && <div className="faint small">{hint}{children}</div>}
    </div>
  );
};

/** Caps usage (Bot 3 live_limits): per order, daily loss (distance to the auto e-stop), traded today, open exposure. */
export function CapsPanel({ status, decisions }: { status: StatusSnapshot | null; decisions: Decision[] }) {
  const { wallet, walletEndpoint } = useLive();
  const L = limitsOf(wallet), W = L.wallet ?? null;
  const pf = status?.portfolio;
  const lastBuy = decisions.find((d) => d.action === "BUY" && d.execution && ["FILLED", "PARTIALLY_FILLED"].includes(d.execution.status));
  const exposure = L.open_exposure_usdt ?? (pf ? (pf.positions ?? []).reduce((a, p) => a + p.quantity * (p.current_price ?? p.avg_entry_price), 0) : null);
  const loss = L.daily_loss_usdt ?? (pf ? Math.max(0, -(pf.daily_pnl ?? 0)) : null);
  const orderCap = L.limit_source ? L.per_order_cap_usdt : stricter(L.per_order_cap_usdt, W?.per_order_cap_usdt); // effective when the backend already merged
  const dayCap = L.limit_source ? L.daily_limit_usdt ?? L.daily_notional_cap_usdt : stricter(L.daily_notional_cap_usdt ?? L.daily_limit_usdt, W?.daily_limit_usdt);
  const which = (ours?: number | null, w?: number | null) => (w == null ? null : <> · agent {usd(ours, 0)} vs wallet {usd(w, 0)}</>);
  const src = (k: "per_order_cap_usdt" | "daily_limit_usdt", ours?: number | null, w?: number | null) => {
    const ls = L.limit_source, s = (typeof ls === "string" ? ls : ls?.[k]) ?? (w == null ? (ours == null ? null : "risk") : (ours ?? Infinity) <= w ? "risk" : "wallet");
    return s ? <span className={`pill ${s === "wallet" ? "warn" : ""}`} style={{ marginLeft: 6, fontSize: ".66rem" }} title={`limit_source: ${s}`}>set by {s === "wallet" ? "wallet" : "agent"}</span> : null;
  };
  const toStop = L.daily_loss_cap_usdt != null && loss != null ? Math.max(0, L.daily_loss_cap_usdt - loss) : null;
  return (
    <Card title="Hard caps" extra={<span className="pill">{L.per_order_cap_usdt != null ? "enforced by risk (Bot 3)" : "not reported"}</span>}>
      {!wallet && walletEndpoint === false && <div className="small muted" style={{ marginBottom: 8 }}>Caps come from <code>GET /api/wallet</code> → <code>limits</code>, which this backend doesn't serve yet.</div>}
      {(L.warnings ?? []).filter(Boolean).map((w) => <div key={w} className="banner amber" style={{ marginBottom: 10 }}><span className="grow small">⚠ {w}</span></div>)}
      <div className="meters">
        <Meter label={<>Per order{src("per_order_cap_usdt", L.per_order_cap_usdt, W?.per_order_cap_usdt)}</>} used={lastBuy?.execution?.notional ?? null} cap={orderCap} hint={<>last buy{lastBuy ? ` ${time(lastBuy.timestamp)}` : ": none yet"}{which(L.per_order_cap_usdt, W?.per_order_cap_usdt)}</>} />
        <Meter label="Daily loss → auto e-stop" used={loss} cap={L.daily_loss_cap_usdt} warnAt={0.6}
          hint={toStop != null ? <><b className={toStop < (L.daily_loss_cap_usdt ?? 0) * 0.4 ? "neg" : ""}>{usd(toStop)}</b> left before the agent stops itself (realized + unrealized)</> : undefined} />
        <Meter label={<>Traded today (UTC){src("daily_limit_usdt", L.daily_notional_cap_usdt ?? L.daily_limit_usdt, W?.daily_limit_usdt)}</>} used={L.daily_used_usdt} cap={dayCap}
          hint={<>{L.daily_remaining_usdt != null ? <>{usd(L.daily_remaining_usdt)} left today · </> : null}Wallet quota resets 05:30 IST (00:00 UTC){which(L.daily_notional_cap_usdt ?? L.daily_limit_usdt, W?.daily_limit_usdt)}</>} />
        <Meter label="Open at once" used={exposure} cap={L.max_exposure_usdt} />
      </div>
      <div className="kv" style={{ marginTop: 12 }}>
        <span className="k">Max slippage</span><span>{L.max_slippage_pct != null ? `${L.max_slippage_pct}%` : NA}</span>
        <span className="k">Stale data after</span><span>{L.stale_after_s != null ? `${L.stale_after_s}s` : NA}</span>
        <span className="k">BNB gas reserve</span><span>{L.bnb_gas_reserve ?? wallet?.bnb_gas_reserve ?? NA}{(L.bnb_gas_reserve ?? wallet?.bnb_gas_reserve) != null ? " BNB" : ""}</span>
        <span className="k">Tradable tokens</span><span>{allowedTokensText(L.tradable_tokens) ?? NA}</span>
        <span className="k">Allowed by wallet</span><span>{allowedTokensText(W?.allowed_tokens) ?? allowedTokensText(L.allowed_tokens) ?? NA}</span>
      </div>
    </Card>
  );
}

/** Connected wallet (Agentic Wallet `baw`): address, chain, balances and a gas warning. */
export function WalletPanel() {
  const { wallet, walletEndpoint, isLive, system, refresh } = useLive();
  const reserve = wallet?.limits?.bnb_gas_reserve ?? wallet?.bnb_gas_reserve ?? null;
  const bnb = wallet?.balances.find((b) => balAsset(b) === "BNB");
  const gasLow = reserve != null && bnb ? balAmount(bnb) < reserve * 2 : false, gasOut = reserve != null && bnb ? balAmount(bnb) < reserve : false;
  const total = wallet?.balances.reduce((a, b) => a + (balValue(b) ?? 0), 0);
  return (
    <Card title="Wallet" extra={<>{wallet && <span className={`pill ${wallet.connected ? "ok" : "bad"}`}><span className="dot" />{wallet.connected ? "connected" : "disconnected"}</span>}
      <button className="btn ghost" style={{ padding: "3px 9px", marginLeft: 6 }} onClick={refresh} title="Refresh balances">↻</button></>}>
      {!system ? <Skeleton h={90} /> : !wallet ? (
        <div className="small muted">
          {walletEndpoint === false
            ? <>Wallet info is <b>not available</b> from this backend yet (needs <code>GET /api/wallet</code>).{isLive ? " Check balances in the Binance App or on BscScan." : ""}</>
            : isLive ? "No wallet reported." : `${system.trading_mode}: no wallet; trades use simulated cash.`}
        </div>
      ) : (
        <>
          {gasLow && <div className={`banner ${gasOut ? "red" : "amber"}`} style={{ marginBottom: 10 }}><span className="grow small">
            <b>{gasOut ? "Out of gas reserve:" : "BNB gas running low:"}</b> {balAmount(bnb!).toFixed(4)} BNB, reserve {reserve} BNB. {gasOut ? "New buys are blocked until you top up." : "Top up soon or buys will be blocked."}</span></div>}
          <div className="kv">
            <span className="k">Address</span>
            <span>{wallet.address ? <><a className="mono" href={bscAddr(wallet.address)} target="_blank" rel="noreferrer" title={wallet.address}>{shortHex(wallet.address, 8)} ↗</a>
              <button className="btn ghost" style={{ padding: "1px 7px", marginLeft: 6, fontSize: ".72rem" }} onClick={() => navigator.clipboard?.writeText(wallet.address!)}>copy</button></> : NA}</span>
            <span className="k">Provider</span><span>{wallet.provider === "baw" ? "Binance Agentic Wallet (baw)" : wallet.provider ?? NA}</span>
            <span className="k">Network</span><span>{wallet.chain ?? "BSC"} · chain {wallet.chain_id ?? NA}{String(wallet.chain_id) === "56" ? " (mainnet)" : ""}</span>
            <span className="k">Total</span><span><b>{total ? usd(total) : NA}</b>{wallet.updated_at ? <span className="faint small"> · {time(wallet.updated_at)}</span> : null}</span>
          </div>
          <div className="table-wrap" style={{ marginTop: 10, maxHeight: 220 }}>
            <table>
              <thead><tr><th>Asset</th><th className="r">Balance</th><th className="r">Value</th></tr></thead>
              <tbody>
                {wallet.balances.length === 0 && <tr><td colSpan={3} className="faint">no balances reported</td></tr>}
                {wallet.balances.map((b) => (
                  <tr key={balAsset(b)}><td><b>{balAsset(b)}</b></td>
                    <td className={`r${balAsset(b) === "BNB" && gasLow ? " neg" : ""}`}>{balAmount(b).toLocaleString(undefined, { maximumFractionDigits: balAsset(b) === "USDT" ? 2 : 6 })}</td>
                    <td className="r">{balValue(b) != null ? usd(balValue(b)) : NA}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          {api.kind === "mock" && <div className="note">Demo wallet from the mock LIVE scenario.</div>}
        </>
      )}
    </Card>
  );
}

const Flag = ({ v, label, hint }: { v: boolean | null | undefined; label: string; hint?: string }) => (
  <span className={`chip ${v == null ? "unk" : v ? "pass" : "fail"}`} title={hint}>{v == null ? "?" : v ? "✓" : "✕"} {label}</span>
);
/** The live gate: the three switches + wallet, can_trade and the backend's blocked_reasons as they come. */
export function LiveGateCard() {
  const { gate, gateSource, system } = useLive();
  if (!system) return <Card title="Live gate"><Skeleton h={70} /></Card>;
  return (
    <Card title="Live gate" extra={gate && <span className={`pill ${gate.can_trade ? "ok" : "bad"}`}><span className="dot" />{gate.can_trade ? "can trade" : "blocked"}</span>}>
      {!gate ? <div className="small muted">{NA}</div> : (
        <>
          <div className="chips">
            <Flag v={gate.live_trading_enabled} label="LIVE_TRADING_ENABLED" hint="Backend env (Bot 6)" />
            <Flag v={gate.binance_live_enabled} label="BINANCE_LIVE_ENABLED" hint="Exchange env (Bot 4)" />
            <Flag v={gate.risk_allow_live} label="risk allow_live" hint="RiskConfig.allow_live (Bot 3)" />
            <Flag v={gate.wallet_connected} label="wallet connected" hint="baw wallet status" />
          </div>
          {gate.blocked_reasons.length > 0 && <ul className="reasons">{gate.blocked_reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
          {gateSource === "derived" && <div className="note">No live-gate endpoint yet: inferred from <code>/api/system</code> and control state; “?” = not exposed.</div>}
        </>
      )}
    </Card>
  );
}

/** Live activity feed: every decision with its risk checks and execution (route, fill, tx hash + BscScan). */
export function ActivityFeed({ decisions, confirmMode = false }: { decisions: Decision[]; confirmMode?: boolean }) {
  const [only, setOnly] = useState(false);
  const rows = only ? decisions.filter((d) => d.action !== "STAY_OUT" || (d.risk_verdict && !d.risk_verdict.approved)) : decisions;
  return (
    <Card title={<>Activity <span className="muted small" style={{ fontWeight: 400 }}>{rows.length}</span></>}
      extra={<button className={`btn ghost${only ? " on" : ""}`} style={{ padding: "3px 10px" }} onClick={() => setOnly(!only)}>{only ? "Showing trades & blocks" : "All decisions"}</button>} pad={false}>
      <div className="feed">
        {rows.length === 0 && <div className="empty">No decisions yet. The agent logs one every tick.</div>}
        {rows.map((d) => {
          const ex = d.execution as (ExecutionResult & LiveExecutionFields) | null | undefined;
          const failed = (d.risk_verdict?.checks ?? []).filter((c) => !c.passed), passed = (d.risk_verdict?.checks ?? []).filter((c) => c.passed).length;
          return (
            <div key={d.id} className="feed-row">
              <div className="feed-time small muted">{time(d.timestamp)}</div>
              <div className="feed-body">
                <div className="feed-head"><ActionBadge action={d.action} /><b>{d.symbol}</b><FamilyBadge symbol={d.symbol} /><span className="small muted trunc">{strategyLabel(d.strategy)}</span>
                  {ex?.real_funds && <span className="pill bad" title="Real funds on mainnet">real funds</span>}
                  {isExitQueued(d.reason) && <span className="pill warn">Exit queued until fill</span>}
                  {/\border_in_flight\b/i.test(d.reason ?? "") && <span className="pill warn">Earlier swap still settling, waiting</span>}</div>
                <div className="small">{d.explanation ?? d.reason}</div>{d.explanation && d.explanation !== d.reason && <div className="faint small">{d.reason}</div>}
                {(failed.length > 0 || passed > 0) && (
                  <div className="chips" style={{ marginTop: 4 }}>
                    {failed.map((c) => <span key={c.name} className="chip fail" title={c.detail ?? ""}>✕ {conditionLabel(c.name)}{c.detail ? `: ${c.detail}` : ""}</span>)}
                    {passed > 0 && <span className="chip pass" title={(d.risk_verdict?.checks ?? []).filter((c) => c.passed).map((c) => `${c.name}: ${c.detail ?? "ok"}`).join("\n")}>✓ {passed} risk check{passed > 1 ? "s" : ""} passed</span>}
                  </div>
                )}
                {[...exchangeCodeLabels(d.reason), ...exchangeCodeLabels(ex?.error)].filter((v, i, a) => a.indexOf(v) === i).map((l) => <span key={l} className="chip fail" style={{ marginTop: 4 }}>✕ {l}</span>)}
                {ex && (ex.status !== "PENDING_CONFIRMATION" || !confirmMode) && (
                  <div className="feed-exec small">
                    <span className={ex.status === "FILLED" ? "pos" : ex.status === "PARTIALLY_FILLED" || ex.status === "PENDING_CONFIRMATION" ? "" : "neg"}>{ex.status === "PENDING_CONFIRMATION" ? "⏳ waiting on chain" : ex.status.replace(/_/g, " ").toLowerCase()}</span>
                    <span className="pill">{ex.route ?? ex.path}</span>
                    {ex.notional ? <span>{usd(ex.notional)}</span> : null}
                    {ex.avg_price ? <span className="muted">@ {usd(ex.avg_price)}</span> : null}
                    {ex.fee ? <span className="faint">fee {usd(ex.fee, 4)}</span> : null}
                    {ex.price_impact_pct != null ? <span className={Math.abs(ex.price_impact_pct) >= 1 ? "neg" : "faint"} title="Price impact / slippage vs the reference price">impact {ex.price_impact_pct.toFixed(2)}%</span> : null}
                    <TxLink exec={ex} />
                    {ex.error && <span className={cls(-1)}>{ex.error}</span>}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/** API.md v0.7 /api/live/status: chain_pending -> "N swap(s) settling on-chain"; cooldowns -> per-token chips with time left. */
export function InFlight({ cooldowns, pending }: { cooldowns?: LiveCooldown[] | null; pending?: ChainPending[] | null }) {
  const [now, setNow] = useState(Date.now());
  const cds = (Array.isArray(cooldowns) ? cooldowns : []).filter((c) => c && c.symbol);
  const live = cds.filter((c) => { const t = Date.parse(c.until); return !Number.isFinite(t) || t > now; });
  const pend = Array.isArray(pending) ? pending.filter(Boolean) : [];
  useEffect(() => {
    if (!cds.length) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [cds.length]);
  if (!live.length && !pend.length) return null;
  const left = (until: string) => {
    const t = Date.parse(until);
    if (!Number.isFinite(t)) return until;
    const s = Math.max(0, Math.round((t - now) / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${String(sec).padStart(2, "0")}s` : `${sec}s`;
  };
  const total = pend.reduce((a, p) => a + (Number(p.notional) || 0), 0);
  return (
    <div className="inflight">
      {pend.length > 0 && (
        <div className="inflight-row">
          <span className="pill warn" title={pend.map((p) => `${p.symbol}${p.notional != null ? ` ${usd(p.notional)}` : ""}${p.since ? ` since ${time(p.since)}` : ""}${p.held_exit ? " (exit queued until fill)" : ""}`).join("\n")}>
            <span className="spin" style={{ width: 10, height: 10 }} /> {pend.length} swap{pend.length > 1 ? "s" : ""} settling on-chain
          </span>
          <span className="small muted">
            {total > 0 ? `${usd(total)} in flight · ` : ""}
            {pend.map((p, i) => <span key={p.client_order_id ?? p.order_id ?? i}>{i ? ", " : ""}{p.symbol}{p.tx_hash ? <> <TxLink exec={{ tx_hash: p.tx_hash } as ExecutionResult & LiveExecutionFields} /></> : null}{p.held_exit ? " (exit queued)" : ""}</span>)}
          </span>
        </div>
      )}
      {live.length > 0 && (
        <div className="inflight-row">
          <span className="lbl" style={{ margin: 0 }}>Cooldowns</span>
          {live.map((c) => (
            <span key={`${c.symbol}-${c.until}`} className="chip warn" title={[c.code != null ? conditionLabel(String(c.code)) : null, c.reason].filter(Boolean).join(" · ")}>
              ⏳ {c.symbol} · {left(c.until)} left{c.code != null ? <span className="faint"> · {conditionLabel(String(c.code))}</span> : null}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

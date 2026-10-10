import { useCallback, useEffect, useRef, useState } from "react";
import type { Event, StatusSnapshot } from "../contracts";
import { api } from "../api";
import type { Alert } from "../api/types";
import { useToast } from "../components/ui";

/**
 * StatusSnapshot kept fresh from the WS /ws stream: STATUS events carry the full snapshot (on connect, after each
 * tick, activation and control change); other events trigger a debounced refetch; ALERTs become toasts. A slow poll
 * is the fallback when the socket is down.
 */
export function useStatus() {
  const toast = useToast();
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<Event | null>(null);
  const [alertsVersion, setAlertsVersion] = useState(0);
  const [tick, setTick] = useState(0); // bumps on every PRICE event (one per loop tick)
  const pending = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    try { setStatus(await api.getStatus()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);

  useEffect(() => {
    refresh();
    const unsub = api.subscribe((e) => {
      setLastEvent(e);
      if (e.type === "PRICE") setTick((t) => t + 1);
      if (e.type === "STATUS" && e.payload && "control" in e.payload) { setStatus(e.payload as unknown as StatusSnapshot); setError(null); return; }
      if (e.type === "ALERT" && e.payload) {
        const a = e.payload as unknown as Alert;
        toast(`${a.code.replace(/_/g, " ")}: ${a.message}`, a.level === "CRITICAL" ? "error" : a.level === "WARNING" ? "warn" : "info");
        setAlertsVersion((v) => v + 1);
      }
      if (e.type === "DECISION" || e.type === "EXECUTION" || e.type === "CONTROL") {
        window.clearTimeout(pending.current);
        pending.current = window.setTimeout(refresh, 400); // in case no STATUS follows
      }
    });
    const poll = window.setInterval(refresh, 15_000);
    return () => { unsub(); window.clearInterval(poll); window.clearTimeout(pending.current); };
  }, [refresh, toast]);

  return { status, error, refresh, lastEvent, alertsVersion, tick };
}

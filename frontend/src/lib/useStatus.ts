import { useCallback, useEffect, useRef, useState } from "react";
import type { Event, StatusSnapshot } from "../contracts";
import { api } from "../api";

/** StatusSnapshot kept fresh via the event stream (WS /ws) plus a slow poll as a fallback. */
export function useStatus() {
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<Event | null>(null);
  const pending = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    try { setStatus(await api.getStatus()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);

  useEffect(() => {
    refresh();
    const unsub = api.subscribe((e) => {
      setLastEvent(e);
      window.clearTimeout(pending.current);
      pending.current = window.setTimeout(refresh, 120); // coalesce bursts
    });
    const poll = window.setInterval(refresh, 10_000);
    return () => { unsub(); window.clearInterval(poll); window.clearTimeout(pending.current); };
  }, [refresh]);

  return { status, error, refresh, lastEvent };
}

"use client";
import { useEffect, useRef, useState } from "react";
import { WS_BASE, getToken } from "./api";

export type LiveEvent = { type: string; data: any };

/** Connects to the backend WebSocket and keeps the most recent events in memory.
 * Real push from the detection pipeline (see backend app/ws.py) — not polling.
 */
export function useLiveSocket(onEvent?: (e: LiveEvent) => void) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let retryDelay = 1000;

    function connect() {
      if (cancelled) return;
      // Browsers can't attach an Authorization header to a WebSocket
      // handshake, so the token travels as a query param instead (backend
      // validates it before accepting — see main.py's /ws). Read fresh on
      // every (re)connect attempt, not just once, so a login that happens
      // after this hook first mounted is picked up on the next retry.
      const token = getToken();
      const url = token ? `${WS_BASE}/ws?token=${encodeURIComponent(token)}` : `${WS_BASE}/ws`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) setTimeout(connect, retryDelay);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (msg) => {
        try {
          const parsed: LiveEvent = JSON.parse(msg.data);
          setLastEvent(parsed);
          onEvent?.(parsed);
        } catch {}
      };
    }
    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { connected, lastEvent };
}

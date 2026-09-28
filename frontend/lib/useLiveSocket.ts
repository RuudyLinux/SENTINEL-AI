"use client";
import { useEffect, useRef, useState } from "react";
import { WS_BASE, getToken } from "./api";

export type LiveEvent = { type: string; data: any };

/** Backend websocket, keeping recent events in memory. Real push from the
 * pipeline (app/ws.py), not polling.
 */
export function useLiveSocket(onEvent?: (e: LiveEvent) => void) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // The connect effect runs once (deps [] on purpose, reconnecting on every
  // render would be much worse), so it closes over the first render's
  // handler. A ref updated every render means the one socket always calls the
  // current handler instead of a stale one.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let cancelled = false;
    // exponential backoff (bounded) instead of retrying every second forever;
    // resets on every successful open so one blip doesn't leave a long delay
    const BASE_DELAY_MS = 1000;
    const MAX_DELAY_MS = 30000;
    let retryDelay = BASE_DELAY_MS;

    function connect() {
      if (cancelled) return;
      // No Authorization header on a websocket handshake, so the token is a
      // query param (checked before accept, main.py /ws). Read fresh on every
      // attempt so a login after mount is picked up on the next retry.
      const token = getToken();
      const url = token ? `${WS_BASE}/ws?token=${encodeURIComponent(token)}` : `${WS_BASE}/ws`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        // the socket can still be mid-handshake when the component unmounts,
        // and onopen fires on the closing socket afterwards; don't let a stale
        // socket set connected back to true
        if (cancelled) return;
        setConnected(true);
        retryDelay = BASE_DELAY_MS; // recovered, reset the backoff
      };
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(MAX_DELAY_MS, retryDelay * 2);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (msg) => {
        // same guard, no state updates after unmount
        if (cancelled) return;
        try {
          const parsed: LiveEvent = JSON.parse(msg.data);
          setLastEvent(parsed);
          onEventRef.current?.(parsed);
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

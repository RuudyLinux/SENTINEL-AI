"use client";
import { useEffect, useRef, useState } from "react";
import { WS_BASE, getToken } from "./api";

export type LiveEvent = { type: string; data: any };

/** Backend WebSocket (app/ws.py), keeping recent events in memory. */
export function useLiveSocket(onEvent?: (e: LiveEvent) => void) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // The connect effect runs once, so the handler is read through a ref to
  // always call the current one.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let cancelled = false;
    // Bounded exponential backoff, reset after each successful open.
    const BASE_DELAY_MS = 1000;
    const MAX_DELAY_MS = 30000;
    let retryDelay = BASE_DELAY_MS;

    function connect() {
      if (cancelled) return;
      // WebSocket handshakes can't carry an Authorization header, so the token
      // is a query parameter, read fresh on each attempt.
      const token = getToken();
      const url = token ? `${WS_BASE}/ws?token=${encodeURIComponent(token)}` : `${WS_BASE}/ws`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        // Ignore a stale socket that opens after unmount.
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

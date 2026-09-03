"use client";
import { useEffect, useRef, useState } from "react";
import { WS_BASE } from "./api";

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
      const ws = new WebSocket(`${WS_BASE}/ws`);
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

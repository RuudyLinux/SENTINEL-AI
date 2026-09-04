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
    // Self-Heal Part 2 recovery type 5 (WebSocket disconnect): bounded
    // exponential backoff instead of a flat 1s retry — real network/server
    // outages shouldn't be hammered once per second forever. Resets to the
    // base delay on every successful open (see ws.onopen below), so a
    // single blip never leaves the socket permanently on a long delay.
    const BASE_DELAY_MS = 1000;
    const MAX_DELAY_MS = 30000;
    let retryDelay = BASE_DELAY_MS;

    function connect() {
      if (cancelled) return;
      // Browsers can't attach an Authorization header to a WebSocket
      // handshake, so the token travels as a query param instead (backend
      // validates it before accepting — see main.py's /ws). Read fresh on
      // every (re)connect attempt, not just once, so a login that happens
      // after this hook first mounted is picked up on the next retry
      // (preserves JWT auth across every reconnect, not just the first).
      const token = getToken();
      const url = token ? `${WS_BASE}/ws?token=${encodeURIComponent(token)}` : `${WS_BASE}/ws`;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        // Stress-test/audit finding: a socket can still be mid-handshake
        // when the component unmounts (cleanup already ran, cancelled=true,
        // wsRef.current?.close() called) — onopen can fire microtasks later
        // on that now-closing socket. Guarded so a stale socket can never
        // flip `connected` back to true after unmount.
        if (cancelled) return;
        setConnected(true);
        retryDelay = BASE_DELAY_MS; // real recovery — un-backoff for the next disconnect
      };
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(MAX_DELAY_MS, retryDelay * 2);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (msg) => {
        // Same stale-socket guard as onopen — a message racing the cleanup
        // close() must never update state after this hook has unmounted.
        if (cancelled) return;
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

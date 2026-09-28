"use client";
import { useEffect, useState } from "react";

import { buildTokenedStream } from "./api";

/** Keeps an MJPEG URL authorized while it's on screen. The backend stops a
 * stream when its token expires, and both viewers used to fetch a token once
 * on mount, so the picture froze. Refresh is scheduled from the token's own
 * exp since stream_token_ttl_seconds is configurable.
 */
export function useStreamUrl(tokenPath: string, resourcePath: string, enabled: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setUrl(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function load() {
      try {
        const { url: next, expiresAt } = await buildTokenedStream(tokenPath, resourcePath);
        if (cancelled) return;
        setUrl(next);
        // at 80% of the remaining lifetime so the new stream is open before the
        // old one is cut. min 15s so a tiny TTL can't loop, max 15min so a
        // missing exp still refreshes
        const remaining = expiresAt ? expiresAt - Date.now() : 0;
        const delay = remaining > 0 ? Math.max(remaining * 0.8, 15_000) : 15 * 60_000;
        timer = setTimeout(load, delay);
      } catch {
        if (!cancelled) setUrl(null);
      }
    }

    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [tokenPath, resourcePath, enabled]);

  return url;
}

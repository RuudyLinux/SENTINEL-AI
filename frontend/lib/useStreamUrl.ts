"use client";
import { useEffect, useState } from "react";

import { buildTokenedStream } from "./api";

/** Keeps an MJPEG stream URL authorized for as long as it is on screen.
 *
 * The backend stops a stream when the token that opened it expires. Both
 * viewers used to fetch a token once, on mount, and never again — which was
 * invisible while a stream outlived its token indefinitely, and becomes a
 * frozen picture now that it does not. The refresh is scheduled from the
 * token's own `exp` rather than a hardcoded interval, because
 * `stream_token_ttl_seconds` is deployment-configurable.
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
        // Refreshed at 80% of the remaining lifetime, so the replacement
        // stream is already open before the old one is cut. Floored at 15s so
        // a very short TTL cannot turn this into a request loop, and capped
        // at 15min so an absent/unreadable `exp` still refreshes eventually.
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

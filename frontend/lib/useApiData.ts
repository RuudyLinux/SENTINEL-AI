"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

/** Fetches real data from the backend and tracks loading/error state honestly:
 * a failed request surfaces as `error`, never as a silently-empty result that
 * could be mistaken for "there is genuinely no data yet".
 */
export function useApiData<T>(
  path: string | null,
  opts?: { pollMs?: number }
): { data: T | null; loading: boolean; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);

  const load = useCallback(() => {
    if (!path) return;
    const id = ++requestId.current;
    api
      .get<T>(path)
      .then((res) => {
        if (id !== requestId.current) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (id !== requestId.current) return;
        setError(err instanceof ApiError ? err.message : "Could not reach the backend");
      })
      .finally(() => {
        if (id === requestId.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!opts?.pollMs) return;

    // Polling stops while the tab is hidden, and refreshes once as soon as it
    // is shown again.
    //
    // Measured on the running system before this: /dashboard issued exactly
    // the same 7 API requests per 30 seconds whether it was the visible tab or
    // buried behind another one. A control-room workstation leaves these
    // screens open all shift, so the polling that nobody can see was real
    // load — database queries on a host already running YOLO inference for
    // every camera, for a render no one was looking at.
    //
    // The immediate re-fetch on becoming visible is the part that keeps this a
    // pure optimisation: the operator never looks at a screen that quietly
    // stopped updating while it was hidden. Anything genuinely live while
    // hidden (alerts) arrives over the WebSocket, which this does not touch.
    let timer: ReturnType<typeof setInterval> | null = null;

    function start() {
      if (timer !== null) return;
      timer = setInterval(load, opts!.pollMs);
    }
    function stop() {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    }
    function onVisibilityChange() {
      if (document.hidden) {
        stop();
      } else {
        load();
        start();
      }
    }

    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, opts?.pollMs]);

  return { data, loading, error, reload: load };
}

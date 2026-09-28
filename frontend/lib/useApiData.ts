"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

/** Fetches backend data and tracks loading and error state. A failed request
 * is reported as `error`, never as an empty result.
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

    // Polling pauses while the tab is hidden and refreshes once when it becomes
    // visible again; live events arrive over the WebSocket.
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

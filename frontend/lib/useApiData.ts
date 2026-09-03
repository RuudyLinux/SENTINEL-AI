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
    if (opts?.pollMs) {
      const t = setInterval(load, opts.pollMs);
      return () => clearInterval(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, opts?.pollMs]);

  return { data, loading, error, reload: load };
}

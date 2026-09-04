"use client";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import ErrorState from "@/components/ErrorState";

export default function PersonTrackingPage() {
  const params = useSearchParams();
  const [detectionId, setDetectionId] = useState(params.get("detection_id") || "");
  const [minSimilarity, setMinSimilarity] = useState(0.6);
  const [results, setResults] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function search(e?: React.FormEvent) {
    e?.preventDefault();
    if (!detectionId.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.get<any>(
        `/api/persons/${encodeURIComponent(detectionId.trim())}/similar?min_similarity=${minSimilarity}`
      );
      setResults(res.candidates);
    } catch (err) {
      setResults(null);
      setError(
        err instanceof ApiError && err.status === 404
          ? "Detection not found."
          : err instanceof ApiError
          ? err.message
          : "Could not reach the backend."
      );
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (params.get("detection_id")) search();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Cross-Camera Person Tracking</h1>

      <div className="text-xs bg-panel2 border border-border rounded-lg p-3 text-slate-300 max-w-2xl">
        <strong className="text-slate-100">Appearance-similarity candidates only — not identity verification.</strong>{" "}
        Results are ranked by how visually similar a person crop's color signature is to the reference detection
        (clothing/color, roughly) — this is not face recognition and does not identify who anyone is. Review each
        candidate manually before acting on it.
      </div>

      <form onSubmit={search} className="flex gap-2 max-w-xl items-end flex-wrap">
        <div className="flex-1 min-w-[240px]">
          <label className="text-xs text-slate-400">Reference person detection ID</label>
          <input
            value={detectionId}
            onChange={(e) => setDetectionId(e.target.value)}
            placeholder="det_xxxxxxxxxx (from Search / Investigate results)"
            className="block w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent font-mono mt-1"
          />
        </div>
        <div>
          <label className="text-xs text-slate-400">Min similarity</label>
          <input
            type="number" min="0" max="1" step="0.05" value={minSimilarity}
            onChange={(e) => setMinSimilarity(parseFloat(e.target.value) || 0)}
            className="block w-24 bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent mt-1"
          />
        </div>
        <button disabled={busy} className="text-xs bg-accent text-ink font-medium rounded px-4 py-2 disabled:opacity-50">
          {busy ? "Searching..." : "FIND SIMILAR"}
        </button>
      </form>

      {error && <ErrorState message={error} onRetry={() => search()} />}

      {results && (
        <div className="border border-border rounded-lg divide-y divide-border max-w-2xl">
          {results.length === 0 && (
            <div className="px-3 py-4 text-xs text-slate-500">No candidates at or above this similarity threshold.</div>
          )}
          {results.map((r: any) => (
            <div key={r.detection_id} className="px-3 py-2 flex items-center justify-between text-sm">
              <div>
                <span className="font-mono">{r.camera_code}</span>
                <span className="text-slate-400 ml-2">{r.camera_name}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-slate-500">{new Date(r.timestamp).toLocaleString()}</span>
                <span className="text-xs px-2 py-0.5 rounded bg-accent/15 text-accent border border-accent/30">
                  {Math.round(r.similarity * 100)}% similar
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

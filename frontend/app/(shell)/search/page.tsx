"use client";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import ErrorState from "@/components/ErrorState";

export default function SearchPage() {
  const params = useSearchParams();
  const router = useRouter();
  const [q, setQ] = useState(params.get("q") || "");
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(query: string) {
    if (!query.trim()) return;
    setError(null);
    try {
      const res = await api.get<any>(`/api/search?q=${encodeURIComponent(query)}`);
      setResults(res);
    } catch (err) {
      setResults(null);
      setError(err instanceof ApiError ? err.message : "Search request failed");
    }
  }

  useEffect(() => {
    if (params.get("q")) run(params.get("q")!);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    router.push(`/search?q=${encodeURIComponent(q)}`);
    run(q);
  }

  return (
    <div className="space-y-5 max-w-3xl">
      <h1 className="text-lg font-semibold">Global Search</h1>
      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Find GJ05AB1234 after 18:00, or search a camera/incident..."
          className="flex-1 bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">SEARCH</button>
      </form>
      <div className="text-xs text-slate-500">
        Suggested: <span className="text-slate-400">Find GJ05AB1234</span> · <span className="text-slate-400">Show vehicles after 6 PM</span>
      </div>

      {error && <ErrorState message={error} onRetry={() => run(q)} />}

      {results && (
        <div className="space-y-4">
          {Object.keys(results.parsed_filters).length > 1 && (
            <div className="text-xs bg-panel2 border border-border rounded p-2">
              Parsed filters: <code>{JSON.stringify(results.parsed_filters)}</code>
            </div>
          )}
          <ResultSection title="Cameras" items={results.cameras} render={(c: any) => `${c.camera_code} — ${c.name}`} />
          <ResultSection
            title="Vehicles"
            items={results.vehicles}
            render={(v: any) => `${v.plate_text || "Unread plate"} ${v.watchlist_flag ? "⚠ WATCHLIST" : ""}`}
            onClick={(v: any) => router.push(`/vehicles/tracking?vehicle_id=${v.id}`)}
          />
          <ResultSection
            title="Incidents"
            items={results.incidents}
            render={(i: any) => `${i.title} — ${i.status}`}
            onClick={(i: any) => router.push(`/incidents/${i.id}`)}
          />
          <ResultSection
            title="Alerts"
            items={results.alerts}
            render={(a: any) => `${a.severity} alert on ${a.camera_id}`}
            onClick={(a: any) => router.push(`/alerts/${a.id}`)}
          />
        </div>
      )}
    </div>
  );
}

function ResultSection({ title, items, render, onClick }: { title: string; items: any[]; render: (i: any) => string; onClick?: (i: any) => void }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <div className="text-xs uppercase text-slate-500 mb-1">{title}</div>
      <div className="border border-border rounded-lg divide-y divide-border">
        {items.map((item, i) => (
          <div key={i} onClick={() => onClick?.(item)} className={`px-3 py-2 text-sm ${onClick ? "cursor-pointer hover:bg-panel2" : ""}`}>
            {render(item)}
          </div>
        ))}
      </div>
    </div>
  );
}

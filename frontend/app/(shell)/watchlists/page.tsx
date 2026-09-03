"use client";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

const TABS = ["plate", "vehicle", "person"] as const;

export default function WatchlistsPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("plate");
  const { data: entriesData, error, reload } = useApiData<any[]>(`/api/watchlists?entity_type=${tab}`);
  const entries = entriesData || [];
  const [form, setForm] = useState({ identifier: "", reason: "", priority: "HIGH" });
  const [actionError, setActionError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setActionError(null);
    try {
      await api.post("/api/watchlists", { ...form, entity_type: tab });
      setForm({ identifier: "", reason: "", priority: "HIGH" });
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not save watchlist entry");
    }
  }

  async function remove(id: string) {
    setActionError(null);
    try {
      await api.del(`/api/watchlists/${id}`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not deactivate entry");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Watchlists</h1>
      <div className="flex gap-2">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`text-xs px-3 py-1.5 rounded border ${tab === t ? "border-accent text-accent" : "border-border text-slate-400"}`}>
            {t.toUpperCase()}S
          </button>
        ))}
      </div>

      <form onSubmit={create} className="bg-panel border border-border rounded-lg p-4 flex gap-2 items-end max-w-2xl">
        <div className="flex-1">
          <label className="text-xs text-slate-400">{tab === "plate" ? "Plate number" : tab === "vehicle" ? "Vehicle identifier" : "Person identifier / note"}</label>
          <input required value={form.identifier} onChange={(e) => setForm({ ...form, identifier: e.target.value.toUpperCase() })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" />
        </div>
        <div className="flex-1">
          <label className="text-xs text-slate-400">Reason</label>
          <input value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" />
        </div>
        <div>
          <label className="text-xs text-slate-400">Priority</label>
          <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
            <option>LOW</option><option>MEDIUM</option><option>HIGH</option><option>CRITICAL</option>
          </select>
        </div>
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2 h-fit">SAVE</button>
      </form>
      {actionError && <div className="text-xs text-critical">{actionError}</div>}

      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : entries.length === 0 ? (
        <EmptyState title={`No ${tab} watchlist entries`} />
      ) : (
        <div className="border border-border rounded-lg divide-y divide-border">
          {entries.map((e) => (
            <div key={e.id} className="px-3 py-2 flex items-center justify-between text-sm">
              <div>
                <span className="font-mono">{e.identifier}</span>
                <span className="text-xs text-slate-500 ml-2">{e.reason}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className={`severity-${e.priority} text-xs`}>{e.priority}</span>
                <button onClick={() => remove(e.id)} className="text-xs text-slate-500 hover:text-critical">Deactivate</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

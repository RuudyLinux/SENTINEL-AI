"use client";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

export default function AiRulesPage() {
  const { data: rulesData, error: rulesError, reload: reloadRules } = useApiData<any[]>("/api/rules");
  const { data: zonesData, error: zonesError } = useApiData<any[]>("/api/zones");
  const rules = rulesData || [];
  const zones = zonesData || [];
  const [form, setForm] = useState({ name: "", rule_type: "watchlist_plate", zone_id: "", priority: "CRITICAL" });
  const [actionError, setActionError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setActionError(null);
    try {
      await api.post("/api/rules", { ...form, zone_id: form.zone_id || null });
      setForm({ name: "", rule_type: "watchlist_plate", zone_id: "", priority: "CRITICAL" });
      reloadRules();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create rule");
    }
  }

  async function disable(id: string) {
    setActionError(null);
    try {
      await api.post(`/api/rules/${id}/disable`);
      reloadRules();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not disable rule");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">AI Rule Engine</h1>
      <p className="text-xs text-slate-500 max-w-2xl">
        Three rule types drive the real alert pipeline: <code>watchlist_plate</code> (fires when an ANPR read matches
        an active watchlist plate), <code>zone_entry</code> (fires when a detection's bounding-box center falls
        inside a restricted zone on the linked camera), and <code>loitering</code> (fires when the same tracked
        object dwells continuously inside a zone past its configured threshold — set the zone's loitering seconds
        on the Map screen's Restricted Zones tab first). All three respect the zone's schedule window
        (schedule_start/schedule_end) and are evaluated live against real detections — see
        backend/app/pipeline/rules_engine.py.
      </p>

      <form onSubmit={create} className="bg-panel border border-border rounded-lg p-4 flex gap-2 items-end flex-wrap max-w-3xl">
        <div>
          <label className="text-xs text-slate-400">Rule name</label>
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" />
        </div>
        <div>
          <label className="text-xs text-slate-400">Type</label>
          <select value={form.rule_type} onChange={(e) => setForm({ ...form, rule_type: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
            <option value="watchlist_plate">Watchlist Plate Match</option>
            <option value="zone_entry">Restricted Zone Entry</option>
            <option value="loitering">Loitering (dwell time)</option>
          </select>
        </div>
        {(form.rule_type === "zone_entry" || form.rule_type === "loitering") && (
          <div>
            <label className="text-xs text-slate-400">Zone</label>
            <select required value={form.zone_id} onChange={(e) => setForm({ ...form, zone_id: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
              <option value="">Select zone...</option>
              {zones.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}{form.rule_type === "loitering" && !z.loitering_seconds ? " (no loitering threshold set)" : ""}
                </option>
              ))}
            </select>
            {zonesError && <div className="text-xs text-critical mt-1">Zone list unavailable: {zonesError}</div>}
            {form.rule_type === "loitering" && (
              <div className="text-xs text-slate-500 mt-1">
                Zones with no threshold set won't fire — set one on Map → Restricted Zones first.
              </div>
            )}
          </div>
        )}
        <div>
          <label className="text-xs text-slate-400">Priority</label>
          <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
            <option>LOW</option><option>MEDIUM</option><option>HIGH</option><option>CRITICAL</option>
          </select>
        </div>
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">CREATE RULE</button>
      </form>
      {actionError && <div className="text-xs text-critical">{actionError}</div>}

      {rulesError ? (
        <ErrorState message={rulesError} onRetry={reloadRules} />
      ) : rules.length === 0 ? (
        <EmptyState title="No rules yet" hint="A default watchlist_plate check always runs even without an explicit rule row." />
      ) : (
        <div className="border border-border rounded-lg divide-y divide-border">
          {rules.map((r) => (
            <div key={r.id} className="px-3 py-2 flex items-center justify-between text-sm">
              <span>{r.name} <span className="text-xs text-slate-500">({r.rule_type})</span></span>
              <div className="flex items-center gap-3">
                <span className={`severity-${r.priority} text-xs`}>{r.priority}</span>
                <span className="text-xs text-slate-500">{r.active ? "active" : "disabled"}</span>
                {r.active && <button onClick={() => disable(r.id)} className="text-xs text-slate-500 hover:text-critical">Disable</button>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

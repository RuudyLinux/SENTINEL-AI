"use client";
import { useState } from "react";
import dynamic from "next/dynamic";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

const CameraMap = dynamic(() => import("@/components/CameraMap"), { ssr: false });

const TABS = ["Live Map", "Incident Map", "Restricted Zones"] as const;

export default function MapIntelligencePage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Live Map");
  const { data: camerasData, error: camerasError, reload: reloadCameras } = useApiData<any[]>("/api/cameras");
  const { data: incidentsData, error: incidentsError } = useApiData<any[]>("/api/incidents");
  const { data: zonesData, error: zonesError, reload: reloadZones } = useApiData<any[]>("/api/zones");
  const [zoneForm, setZoneForm] = useState({ name: "", camera_id: "", severity: "HIGH" });
  const [formError, setFormError] = useState<string | null>(null);

  const cameras = camerasData || [];
  const incidents = incidentsData || [];
  const zones = zonesData || [];

  const incidentCameraIds = new Set(incidents.filter((i) => i.status !== "closed").map((i) => i.camera_id));
  const incidentCameras = cameras.filter((c) => incidentCameraIds.has(c.id));

  async function createZone(e: React.FormEvent) {
    e.preventDefault();
    if (!zoneForm.camera_id) return;
    setFormError(null);
    try {
      await api.post("/api/zones", { ...zoneForm, x1: 0, y1: 0, x2: 1, y2: 1, zone_type: "restricted" });
      setZoneForm({ name: "", camera_id: "", severity: "HIGH" });
      reloadZones();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not create zone");
    }
  }

  return (
    <div className="space-y-4 h-full flex flex-col">
      <h1 className="text-lg font-semibold">Map Intelligence</h1>
      <div className="flex gap-2 border-b border-border">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`text-xs px-3 py-2 border-b-2 ${tab === t ? "border-accent text-accent" : "border-transparent text-slate-400"}`}>
            {t}
          </button>
        ))}
      </div>

      {tab === "Live Map" && (
        camerasError ? (
          <ErrorState message={camerasError} onRetry={reloadCameras} />
        ) : (
          <div className="flex-1 min-h-[450px] border border-border rounded-lg overflow-hidden">
            <CameraMap cameras={cameras} />
          </div>
        )
      )}

      {tab === "Incident Map" && (
        camerasError || incidentsError ? (
          <ErrorState message={camerasError || incidentsError || "Data unavailable"} onRetry={reloadCameras} />
        ) : incidentCameras.length === 0 ? (
          <EmptyState title="No active incidents to map" />
        ) : (
          <div className="flex-1 min-h-[450px] border border-border rounded-lg overflow-hidden">
            <CameraMap cameras={incidentCameras} />
          </div>
        )
      )}

      {tab === "Restricted Zones" && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <form onSubmit={createZone} className="bg-panel border border-border rounded-lg p-4 space-y-3 h-fit">
            <div className="text-sm font-medium">Create Zone</div>
            <input required placeholder="Zone name" value={zoneForm.name} onChange={(e) => setZoneForm({ ...zoneForm, name: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm" />
            <select required value={zoneForm.camera_id} onChange={(e) => setZoneForm({ ...zoneForm, camera_id: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm">
              <option value="">Select camera...</option>
              {cameras.map((c) => <option key={c.id} value={c.id}>{c.camera_code} — {c.name}</option>)}
            </select>
            <select value={zoneForm.severity} onChange={(e) => setZoneForm({ ...zoneForm, severity: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm">
              <option>LOW</option><option>MEDIUM</option><option>HIGH</option><option>CRITICAL</option>
            </select>
            <div className="text-xs text-slate-500">Zone covers the full camera frame in this build (axis-aligned rectangle drawing is a documented non-goal).</div>
            {formError && <div className="text-xs text-critical">{formError}</div>}
            <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">SAVE ZONE</button>
          </form>
          {zonesError ? (
            <ErrorState message={zonesError} onRetry={reloadZones} />
          ) : (
            <div className="border border-border rounded-lg divide-y divide-border h-fit">
              {zones.map((z) => (
                <div key={z.id} className="px-3 py-2 flex justify-between text-sm">
                  <span>{z.name} <span className="text-xs text-slate-500">({cameras.find((c) => c.id === z.camera_id)?.camera_code})</span></span>
                  <span className={`severity-${z.severity} text-xs`}>{z.severity}</span>
                </div>
              ))}
              {zones.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">No zones defined.</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

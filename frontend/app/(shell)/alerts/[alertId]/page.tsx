"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import SeverityBadge from "@/components/SeverityBadge";
import ErrorState from "@/components/ErrorState";

export default function AlertDetailPage() {
  const { alertId } = useParams<{ alertId: string }>();
  const router = useRouter();
  const { data: alert, error, reload } = useApiData<any>(`/api/alerts/${alertId}`);
  const [camera, setCamera] = useState<any>(null);
  const [vehicle, setVehicle] = useState<any>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (!alert) return;
    api.get<any>(`/api/cameras/${alert.camera_id}`).then(setCamera).catch(() => {});
    if (alert.vehicle_id) api.get<any>(`/api/vehicles/${alert.vehicle_id}`).then(setVehicle).catch(() => {});
  }, [alert]);

  async function act(action: string) {
    setActionError(null);
    try {
      await api.post(`/api/alerts/${alertId}/${action}`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Could not ${action} alert`);
    }
  }

  async function createIncident() {
    setActionError(null);
    try {
      const inc = await api.post<any>("/api/incidents", {
        title: `Alert follow-up — ${camera?.camera_code || alert.camera_id}`,
        incident_type: "alert_followup",
        priority: alert.severity,
        camera_id: alert.camera_id,
        alert_id: alert.id,
        vehicle_id: alert.vehicle_id,
        description: alert.reasons.join("; "),
      });
      router.push(`/incidents/${inc.id}`);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create incident");
    }
  }

  if (error) return <ErrorState message={`Alert ${alertId} could not be loaded: ${error}`} onRetry={reload} />;
  if (!alert) return null;

  return (
    <div className="max-w-3xl space-y-5">
      <div className="flex items-center gap-3">
        <SeverityBadge severity={alert.severity} />
        <h1 className="text-lg font-semibold">Alert Detail</h1>
        <span className="text-xs text-slate-500 ml-auto">{alert.status.toUpperCase()}</span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm bg-panel border border-border rounded-lg p-4">
        <div><span className="text-slate-500">What happened?</span><br />{alert.reasons[0]}</div>
        <div><span className="text-slate-500">Where?</span><br />{camera?.camera_code || alert.camera_id} — {camera?.location || "location unavailable"}</div>
        <div><span className="text-slate-500">When?</span><br />{new Date(alert.timestamp).toLocaleString()}</div>
        <div><span className="text-slate-500">Confidence</span><br />{(alert.confidence * 100).toFixed(0)}%</div>
      </div>

      <div className="bg-panel border border-border rounded-lg p-4">
        <div className="text-xs text-slate-500 mb-2">Why was it flagged?</div>
        <ul className="list-disc list-inside text-sm space-y-1">
          {alert.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}
        </ul>
      </div>

      {vehicle && (
        <div className="bg-panel border border-border rounded-lg p-4 text-sm">
          <div className="text-xs text-slate-500 mb-1">Related Vehicle</div>
          <div className="font-mono">{vehicle.plate_text} {vehicle.watchlist_flag && <span className="text-critical">⚠ WATCHLIST</span>}</div>
          <button onClick={() => router.push(`/vehicles/tracking?vehicle_id=${vehicle.id}`)} className="text-xs text-accent hover:underline mt-1">
            VIEW TRAJECTORY →
          </button>
        </div>
      )}

      {actionError && <div className="text-xs text-critical">{actionError}</div>}
      <div className="flex gap-2">
        <button onClick={() => act("acknowledge")} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">ACKNOWLEDGE</button>
        <button onClick={() => act("escalate")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">ESCALATE</button>
        <button onClick={createIncident} className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">CREATE INCIDENT</button>
        <button onClick={() => act("dismiss")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-critical text-critical ml-auto">DISMISS</button>
      </div>
    </div>
  );
}

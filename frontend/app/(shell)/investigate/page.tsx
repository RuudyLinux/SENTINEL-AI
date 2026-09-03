"use client";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { API_BASE } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import SeverityBadge from "@/components/SeverityBadge";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

const CameraMap = dynamic(() => import("@/components/CameraMap"), { ssr: false });

export default function InvestigationWorkspacePage() {
  const router = useRouter();
  const params = useSearchParams();
  const caseId = params.get("case");

  const { data: incidentsData, error: incidentsError, reload: reloadIncidents } = useApiData<any[]>("/api/incidents");
  const { data: camerasData, error: camerasError, reload: reloadCameras } = useApiData<any[]>("/api/cameras");
  const { data: incident, error: incidentError, reload: reloadIncident } = useApiData<any>(caseId ? `/api/incidents/${caseId}` : null);
  const { data: timelineData, error: timelineError, reload: reloadTimeline } = useApiData<any>(caseId ? `/api/incidents/${caseId}/timeline` : null);
  const { data: route, error: routeError } = useApiData<any>(incident?.vehicle_id ? `/api/vehicles/${incident.vehicle_id}/route` : null);

  const incidents = incidentsData || [];
  const cameras = camerasData || [];
  const timeline = timelineData?.events || [];

  if (!caseId) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Investigation Workspace</h1>
        {incidentsError ? (
          <ErrorState message={incidentsError} onRetry={reloadIncidents} />
        ) : incidents.length === 0 ? (
          <EmptyState title="No cases yet" hint="Cases come from incidents. Create one from an alert or camera view." />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border">
            {incidents.map((i) => (
              <div key={i.id} onClick={() => router.push(`/investigate?case=${i.id}`)} className="px-3 py-2 flex items-center justify-between text-sm cursor-pointer hover:bg-panel2">
                <span>{i.title}</span>
                <div className="flex items-center gap-2">
                  <SeverityBadge severity={i.priority} />
                  <span className="text-xs text-slate-500">{i.status}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (incidentError) {
    return <ErrorState message={`Case ${caseId} could not be loaded: ${incidentError}`} onRetry={reloadIncident} />;
  }

  const mapCameras = cameras.filter((c) => route?.sightings.some((s: any) => s.camera_id === c.id));
  const routePoints = route?.sightings
    .map((s: any) => {
      const cam = cameras.find((c) => c.id === s.camera_id);
      return cam ? { lat: cam.lat, lng: cam.lng, label: s.camera_code } : null;
    })
    .filter(Boolean) || [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-xs text-slate-500">CASE: {caseId?.toUpperCase()}</div>
          <h1 className="text-lg font-semibold">{incident?.title}</h1>
        </div>
        <div className="flex gap-2">
          <a href={`${API_BASE}/api/evidence/incidents/${caseId}/package?fmt=json`} target="_blank" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">EXPORT REPORT</a>
          <button onClick={() => router.push("/investigate")} className="text-xs border border-border rounded px-3 py-1.5">BACK TO CASES</button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <h2 className="text-sm font-medium mb-2">Timeline</h2>
          {timelineError ? (
            <ErrorState message={timelineError} onRetry={reloadTimeline} />
          ) : (
            <div className="border border-border rounded-lg divide-y divide-border max-h-96 overflow-y-auto">
              {timeline.map((e: any, i: number) => (
                <div key={i} className="px-3 py-2 flex justify-between text-sm">
                  <span>{e.label}</span>
                  <span className="text-xs text-slate-500">{new Date(e.timestamp).toLocaleTimeString()}</span>
                </div>
              ))}
              {timeline.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">No timeline events yet.</div>}
            </div>
          )}
        </div>
        <div className="min-h-[350px] border border-border rounded-lg overflow-hidden">
          {camerasError || routeError ? (
            <ErrorState message={camerasError || routeError || "Route unavailable"} onRetry={reloadCameras} />
          ) : routePoints.length > 0 ? (
            <CameraMap cameras={mapCameras} route={routePoints} />
          ) : (
            <div className="h-full flex items-center justify-center text-xs text-slate-500">No vehicle route linked to this case.</div>
          )}
        </div>
      </div>
    </div>
  );
}

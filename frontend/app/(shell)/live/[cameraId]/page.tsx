"use client";
import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, API_BASE, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import StatusDot from "@/components/StatusDot";
import ErrorState from "@/components/ErrorState";

export default function SingleCameraPage() {
  const { cameraId } = useParams<{ cameraId: string }>();
  const router = useRouter();
  const { data: camera, loading: cameraLoading, error: cameraError, reload: reloadCamera } = useApiData<any>(
    `/api/cameras/${cameraId}`, { pollMs: 4000 }
  );
  const { data: detections, error: detectionsError, reload: reloadDetections } = useApiData<any[]>(
    `/api/detections?camera_id=${cameraId}&limit=50`, { pollMs: 4000 }
  );
  const [actionError, setActionError] = useState<string | null>(null);

  const detectionRows = detections || [];
  const counts = detectionRows.reduce((acc: Record<string, number>, d) => {
    acc[d.cls] = (acc[d.cls] || 0) + 1;
    return acc;
  }, {});

  async function createIncident() {
    setActionError(null);
    try {
      const inc = await api.post<any>("/api/incidents", {
        title: `Manual incident from ${camera?.camera_code}`,
        incident_type: "manual",
        priority: "MEDIUM",
        location: camera?.location,
        camera_id: cameraId,
        description: "Created by operator from Single Camera View.",
      });
      router.push(`/incidents/${inc.id}`);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create incident");
    }
  }

  if (cameraError) {
    return <ErrorState message={`Camera ${cameraId} could not be loaded: ${cameraError}`} onRetry={reloadCamera} />;
  }
  if (cameraLoading || !camera) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">{camera.name} <span className="text-slate-500 font-normal">({camera.camera_code})</span></h1>
          <div className="text-xs text-slate-400">{camera.location} · {camera.department}</div>
        </div>
        <StatusDot status={camera.status} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_280px] gap-4">
        <div className="bg-black rounded-lg overflow-hidden border border-border aspect-video flex items-center justify-center">
          {camera.status === "online" ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={`${API_BASE}/api/streams/${cameraId}/mjpeg`} alt={camera.name} className="w-full h-full object-contain" />
          ) : (
            <div className="text-slate-500 text-sm">Camera {camera.status}. Last frame: {camera.last_frame_at ? new Date(camera.last_frame_at).toLocaleString() : "never"}</div>
          )}
        </div>

        <div className="space-y-3">
          <div className="border border-border rounded-lg p-3 bg-panel">
            <div className="text-xs uppercase text-slate-400 mb-2">AI Intelligence Panel</div>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div>PERSONS <span className="float-right font-semibold">{counts["person"] || 0}</span></div>
              <div>CARS <span className="float-right font-semibold">{counts["car"] || 0}</span></div>
              <div>TRUCKS <span className="float-right font-semibold">{counts["truck"] || 0}</span></div>
              <div>BUSES <span className="float-right font-semibold">{counts["bus"] || 0}</span></div>
            </div>
          </div>
          <div className="border border-border rounded-lg p-3 bg-panel text-xs text-slate-400 space-y-1">
            <div>FPS: {camera.fps?.toFixed(1) ?? "—"}</div>
            <div>Resolution: {camera.resolution || "—"}</div>
            <div>Errors: {camera.error_count}</div>
          </div>
          {actionError && <div className="text-xs text-critical">{actionError}</div>}
          <div className="flex flex-col gap-2">
            <button onClick={createIncident} className="text-xs bg-accent text-ink font-medium rounded py-2">CREATE INCIDENT</button>
            <button onClick={() => router.push(`/vehicles/tracking`)} className="text-xs border border-border rounded py-2 hover:border-accent">TRACK OBJECT</button>
            <button onClick={() => router.push(`/alerts?camera_id=${cameraId}`)} className="text-xs border border-border rounded py-2 hover:border-accent">OPEN ALERTS</button>
          </div>
        </div>
      </div>

      <div>
        <h2 className="text-sm font-medium text-slate-300 mb-2">Recent Detections</h2>
        {detectionsError ? (
          <ErrorState message={detectionsError} onRetry={reloadDetections} />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border max-h-64 overflow-y-auto">
            {detectionRows.slice(0, 20).map((d) => (
              <div key={d.id} className="px-3 py-1.5 flex justify-between text-xs">
                <span>{d.cls} · {(d.confidence * 100).toFixed(0)}% confidence</span>
                <span className="text-slate-500">{new Date(d.timestamp).toLocaleTimeString()}</span>
              </div>
            ))}
            {detectionRows.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">No detections yet.</div>}
          </div>
        )}
      </div>
    </div>
  );
}

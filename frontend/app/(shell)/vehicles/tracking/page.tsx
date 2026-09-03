"use client";
import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";

const CameraMap = dynamic(() => import("@/components/CameraMap"), { ssr: false });

export default function VehicleTrackingPage() {
  const router = useRouter();
  const params = useSearchParams();
  const vehicleId = params.get("vehicle_id");
  const [plateInput, setPlateInput] = useState("");
  const [route, setRoute] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const { data: camerasData, error: camerasError, reload: reloadCameras } = useApiData<any[]>("/api/cameras");
  const cameras = camerasData || [];

  function loadRoute() {
    if (!vehicleId) return;
    setError(null);
    api
      .get<any>(`/api/vehicles/${vehicleId}/route`)
      .then(setRoute)
      .catch((err) => {
        setError(
          err instanceof ApiError && err.status === 404
            ? "Vehicle not found."
            : err instanceof ApiError
            ? err.message
            : "Could not reach the backend."
        );
      });
  }

  useEffect(loadRoute, [vehicleId]);

  async function trackByPlate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const vehicles = await api.get<any[]>(`/api/vehicles?plate=${encodeURIComponent(plateInput)}`);
      if (vehicles.length === 0) {
        setError("No vehicle found with that plate yet.");
        return;
      }
      router.push(`/vehicles/tracking?vehicle_id=${vehicles[0].id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Search request failed");
    }
  }

  const mapCameras = cameras.filter((c) => route?.sightings.some((s: any) => s.camera_id === c.id));
  const routePoints = route?.sightings
    .map((s: any) => {
      const cam = cameras.find((c) => c.id === s.camera_id);
      return cam ? { lat: cam.lat, lng: cam.lng, label: `${s.camera_code} — ${new Date(s.timestamp).toLocaleTimeString()}` } : null;
    })
    .filter(Boolean) || [];

  async function createIncidentFromRoute() {
    if (!route) return;
    setError(null);
    try {
      const inc = await api.post<any>("/api/incidents", {
        title: `Vehicle tracking — ${route.vehicle.plate_text}`,
        incident_type: "vehicle_tracking",
        priority: route.vehicle.watchlist_flag ? "CRITICAL" : "MEDIUM",
        vehicle_id: route.vehicle.id,
        description: `Cross-camera route across ${route.sightings.length} camera(s).`,
      });
      router.push(`/incidents/${inc.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create incident");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Cross-Camera Vehicle Tracking</h1>
      <form onSubmit={trackByPlate} className="flex gap-2 max-w-md">
        <input
          value={plateInput}
          onChange={(e) => setPlateInput(e.target.value.toUpperCase())}
          placeholder="GJ05AB1234"
          className="flex-1 bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent font-mono"
        />
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">TRACK</button>
      </form>
      {error && <div className="text-xs text-critical">{error}</div>}
      {camerasError && <ErrorState message={`Camera list unavailable: ${camerasError}`} onRetry={reloadCameras} />}

      {route && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div>
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-sm font-medium">
                {route.vehicle.plate_text} {route.vehicle.watchlist_flag && <span className="text-critical">⚠ WATCHLIST MATCH</span>}
              </h2>
              <button onClick={createIncidentFromRoute} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">CREATE INCIDENT</button>
            </div>
            <div className="border border-border rounded-lg divide-y divide-border">
              {route.sightings.map((s: any, i: number) => (
                <div key={i} className="px-3 py-2 flex justify-between text-sm">
                  <span className="font-mono">{s.camera_code}</span>
                  <span className="text-slate-400">{s.camera_name}</span>
                  <span className="text-xs text-slate-500">{new Date(s.timestamp).toLocaleString()}</span>
                </div>
              ))}
              {route.sightings.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">Only one sighting so far — route needs a second camera to detect the same plate.</div>}
            </div>
          </div>
          <div className="min-h-[350px] border border-border rounded-lg overflow-hidden">
            <CameraMap cameras={mapCameras} route={routePoints} />
          </div>
        </div>
      )}
    </div>
  );
}

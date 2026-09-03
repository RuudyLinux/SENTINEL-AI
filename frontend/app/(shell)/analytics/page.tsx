"use client";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from "recharts";
import { useApiData } from "@/lib/useApiData";
import KpiCard from "@/components/KpiCard";
import ErrorState from "@/components/ErrorState";

export default function AnalyticsPage() {
  const { data: overview, error: overviewError, reload: reloadOverview } = useApiData<any>("/api/analytics/overview");
  const { data: eventsByHour, error: eventsError, reload: reloadEvents } = useApiData<any[]>("/api/analytics/events-by-hour");
  const { data: alertsByType, error: alertsTypeError, reload: reloadAlertsType } = useApiData<any[]>("/api/analytics/alerts-by-type");
  const { data: cameraUptime, error: uptimeError, reload: reloadUptime } = useApiData<any[]>("/api/analytics/camera-uptime");
  const { data: aiPerf, error: aiPerfError, reload: reloadAiPerf } = useApiData<any>("/api/analytics/ai-performance");

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Analytics</h1>

      {overviewError ? (
        <ErrorState message={overviewError} onRetry={reloadOverview} />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <KpiCard title="Total AI Events" value={overview?.ai_events.detections_today ?? "—"} />
          <KpiCard title="Active Alerts" value={overview?.alerts.active ?? "—"} />
          <KpiCard title="Open Incidents" value={overview?.incidents.open ?? "—"} />
          <KpiCard title="ANPR Reads" value={overview?.ai_events.plates_today ?? "—"} />
          <KpiCard title="Camera Availability" value={overview ? `${overview.cameras.online}/${overview.cameras.total}` : "—"} />
        </div>
      )}

      <div className="bg-panel border border-border rounded-lg p-4">
        <div className="text-sm font-medium mb-3">Events by Hour (last 24h)</div>
        {eventsError ? (
          <ErrorState message={eventsError} onRetry={reloadEvents} />
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={eventsByHour || []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#22303f" />
                <XAxis dataKey="hour" tick={{ fontSize: 10, fill: "#94a3b8" }} hide />
                <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} />
                <Tooltip contentStyle={{ background: "#161f2c", border: "1px solid #22303f", fontSize: 12 }} />
                <Bar dataKey="count" fill="#2dd4bf" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-panel border border-border rounded-lg p-4">
          <div className="text-sm font-medium mb-3">Alerts by Severity</div>
          {alertsTypeError ? (
            <ErrorState message={alertsTypeError} onRetry={reloadAlertsType} />
          ) : (
            <div className="space-y-2">
              {(alertsByType || []).map((a) => (
                <div key={a.severity} className="flex items-center justify-between text-sm">
                  <span className={`severity-${a.severity}`}>{a.severity}</span>
                  <span>{a.count}</span>
                </div>
              ))}
              {(alertsByType || []).length === 0 && <div className="text-xs text-slate-500">No alerts yet.</div>}
            </div>
          )}
        </div>

        <div className="bg-panel border border-border rounded-lg p-4">
          <div className="text-sm font-medium mb-3">AI Performance (measured, not estimated)</div>
          {aiPerfError ? (
            <ErrorState message={aiPerfError} onRetry={reloadAiPerf} />
          ) : aiPerf ? (
            <div className="text-xs space-y-1">
              <div>Total detections: <b>{aiPerf.total_detections}</b></div>
              <div>Person / Vehicle: <b>{aiPerf.person_detections}</b> / <b>{aiPerf.vehicle_detections}</b></div>
              <div>Average detection confidence: <b>{(aiPerf.average_detection_confidence * 100).toFixed(1)}%</b></div>
              <div>Plate reads (non-empty): <b>{aiPerf.non_empty_plate_reads}</b> / {aiPerf.total_plate_reads}</div>
              <div className="text-slate-500 pt-2">{aiPerf.note}</div>
            </div>
          ) : (
            <div className="text-xs text-slate-500">Loading...</div>
          )}
        </div>
      </div>

      <div className="bg-panel border border-border rounded-lg p-4">
        <div className="text-sm font-medium mb-3">Camera Uptime</div>
        {uptimeError ? (
          <ErrorState message={uptimeError} onRetry={reloadUptime} />
        ) : (
          <div className="divide-y divide-border">
            {(cameraUptime || []).map((c) => (
              <div key={c.camera_code} className="flex justify-between text-sm py-1.5">
                <span className="font-mono">{c.camera_code}</span>
                <span>{c.status} · {c.fps?.toFixed(1)} FPS · {c.error_count} errors</span>
              </div>
            ))}
            {(cameraUptime || []).length === 0 && <div className="text-xs text-slate-500">No cameras registered.</div>}
          </div>
        )}
      </div>
    </div>
  );
}

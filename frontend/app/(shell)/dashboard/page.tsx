"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket } from "@/lib/useLiveSocket";
import KpiCard from "@/components/KpiCard";
import SeverityBadge from "@/components/SeverityBadge";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

export default function DashboardPage() {
  const router = useRouter();
  const [liveFeed, setLiveFeed] = useState<any[]>([]);
  const { data: overview, error, reload } = useApiData<any>("/api/analytics/overview", { pollMs: 10000 });

  useLiveSocket((e) => {
    if (e.type === "alert") {
      setLiveFeed((prev) => [{ ...e.data, kind: "alert" }, ...prev].slice(0, 20));
      reload();
    }
    if (e.type === "detection") {
      setLiveFeed((prev) => [{ ...e.data, kind: "detection" }, ...prev].slice(0, 20));
    }
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Command Center</h1>
        <div className="text-xs text-slate-400">{new Date().toLocaleString()}</div>
      </div>

      {error ? (
        <ErrorState message={`Command Center KPIs could not be loaded: ${error}`} onRetry={reload} />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 stagger-children">
          <KpiCard
            title="Cameras"
            value={overview ? `${overview.cameras.online}/${overview.cameras.total}` : "—"}
            sub="Online / Total"
            onClick={() => router.push("/cameras")}
            buttonLabel="VIEW CAMERAS"
          />
          <KpiCard
            title="Active Alerts"
            value={overview?.alerts.active ?? "—"}
            sub={overview ? `${overview.alerts.critical} Critical · ${overview.alerts.high} High · ${overview.alerts.medium} Medium` : ""}
            onClick={() => router.push("/alerts")}
            buttonLabel="VIEW ALERTS"
          />
          <KpiCard
            title="Open Incidents"
            value={overview?.incidents.open ?? "—"}
            onClick={() => router.push("/incidents")}
            buttonLabel="VIEW INCIDENTS"
          />
          <KpiCard
            title="AI Events Today"
            value={overview?.ai_events.detections_today ?? "—"}
            sub={overview ? `${overview.ai_events.plates_today} plate reads` : ""}
            onClick={() => router.push("/analytics")}
            buttonLabel="VIEW ANALYTICS"
          />
        </div>
      )}

      <div>
        <h2 className="text-sm font-medium text-slate-300 mb-2">Live AI Activity</h2>
        {liveFeed.length === 0 ? (
          <EmptyState
            title="No live activity yet"
            hint="Add a camera under Cameras → Add Camera to start the real detection pipeline."
          />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border max-h-96 overflow-y-auto">
            {liveFeed.map((item, i) => (
              <div key={i} className="px-3 py-2 flex items-center justify-between text-sm">
                <div className="flex items-center gap-3">
                  {item.kind === "alert" ? (
                    <SeverityBadge severity={item.severity} />
                  ) : (
                    <span className="badge bg-slate-500/15 text-slate-300 border border-slate-500/30">{item.cls}</span>
                  )}
                  <span className="text-slate-300">{item.camera_code}</span>
                  {item.kind === "alert" && <span className="text-slate-400 text-xs">{item.reasons?.join("; ")}</span>}
                </div>
                <span className="text-xs text-slate-500">{new Date(item.timestamp).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Wifi, Play, BrainCircuit, RotateCcw, Square, ArrowRight } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket } from "@/lib/useLiveSocket";
import { api } from "@/lib/api";
import KpiCard from "@/components/KpiCard";
import SeverityBadge from "@/components/SeverityBadge";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

const QUICK_ACTIONS: { action: string; label: string; icon: typeof Wifi }[] = [
  { action: "connect", label: "Connect All", icon: Wifi },
  { action: "start", label: "Start All", icon: Play },
  { action: "start_ai", label: "Start AI", icon: BrainCircuit },
  { action: "restart", label: "Restart All", icon: RotateCcw },
  { action: "stop", label: "Stop All", icon: Square },
];

function CameraControlWidget() {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);

  async function quickAction(action: string) {
    // "Restart All"/"Stop All" are disruptive per Camera Control Center's own
    // confirmation rule — this compact widget only offers the full flow
    // (with confirmation) via the deep link, not a bare fire-here button.
    if (action === "restart" || action === "stop") {
      router.push("/cameras/control");
      return;
    }
    setBusy(action);
    try {
      await api.post("/api/cameras/bulk", { action, camera_ids: null });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border border-border rounded-lg bg-panel p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-200">Camera Control</h3>
        <button onClick={() => router.push("/cameras/control")} className="flex items-center gap-1 text-xs text-accent hover:underline">
          Open Camera Control Center <ArrowRight size={12} />
        </button>
      </div>
      <div className="flex flex-wrap gap-2">
        {QUICK_ACTIONS.map(({ action, label, icon: Icon }) => (
          <button
            key={action}
            onClick={() => quickAction(action)}
            disabled={busy === action}
            className="flex items-center gap-1.5 text-xs font-medium border border-border rounded px-2.5 py-1.5 hover:border-accent hover:text-accent transition-colors duration-150 disabled:opacity-40"
          >
            <Icon size={12} strokeWidth={2.25} />
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

function SystemHealthWidget() {
  const router = useRouter();
  const { data } = useApiData<any>("/api/self-heal/health", { pollMs: 15000 });
  return (
    <div className="border border-border rounded-lg bg-panel p-4 space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-200">System Health</h3>
        <button onClick={() => router.push("/self-heal/health")} className="flex items-center gap-1 text-xs text-accent hover:underline">
          Details <ArrowRight size={12} />
        </button>
      </div>
      {!data ? (
        <div className="text-xs text-slate-500">Loading…</div>
      ) : (
        <div className="grid grid-cols-2 gap-1.5 text-xs">
          <span className="text-slate-400">API</span><span className="text-right text-ok">{data.subsystems.api}</span>
          <span className="text-slate-400">Database</span><span className={`text-right ${data.subsystems.database === "HEALTHY" ? "text-ok" : "text-high"}`}>{data.subsystems.database}</span>
          <span className="text-slate-400">Cameras</span><span className="text-right text-slate-200">{data.cameras.online}/{data.cameras.total}</span>
          <span className="text-slate-400">Self-Heal</span><span className="text-right text-ok">{data.subsystems.self_heal}</span>
        </div>
      )}
    </div>
  );
}

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

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <CameraControlWidget />
        <SystemHealthWidget />
      </div>

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

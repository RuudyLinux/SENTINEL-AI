"use client";
import { useRouter } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import KpiCard from "@/components/KpiCard";
import ErrorState from "@/components/ErrorState";

type HealthResponse = {
  timestamp: string;
  subsystems: {
    api: string; database: string; websocket: string; websocket_clients: number;
    ai_engine: string; self_heal: string;
  };
  cameras: { online: number; degraded: number; offline: number; total: number };
  workers_running: number;
  summary: {
    active_problems: number; critical_problems: number; warning_problems: number;
    recovered_today: number; offline_cameras: number; degraded_cameras: number;
  };
};

const SUBSYSTEM_ROWS: { key: keyof HealthResponse["subsystems"]; label: string }[] = [
  { key: "api", label: "API" },
  { key: "database", label: "DATABASE" },
  { key: "websocket", label: "WEBSOCKET" },
  { key: "ai_engine", label: "AI ENGINE" },
  { key: "self_heal", label: "SELF-HEAL" },
];

const OK_VALUES = new Set(["HEALTHY", "CONNECTED", "RUNNING", "ACTIVE"]);

function SubsystemDot({ value }: { value: string }) {
  const ok = OK_VALUES.has(value);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <span className={`h-2 w-2 rounded-full ${ok ? "bg-ok" : "bg-high"}`} aria-hidden="true" />
      <span className={ok ? "text-ok" : "text-high"}>{value}</span>
    </span>
  );
}

export default function SelfHealHealthPage() {
  const router = useRouter();
  const { data, error, reload } = useApiData<HealthResponse>("/api/self-heal/health", { pollMs: 8000 });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">System Health</h1>
          <p className="text-xs text-slate-400 mt-0.5">Real subsystem checks — Sentinel Self-Heal's live view of the platform.</p>
        </div>
      </div>

      {error ? (
        <ErrorState message={`System health could not be loaded: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 stagger-children">
            <KpiCard
              title="Active Problems" value={data.summary.active_problems}
              sub={`${data.summary.critical_problems} critical`}
              onClick={() => router.push("/self-heal/problems")} buttonLabel="VIEW PROBLEMS"
            />
            <KpiCard title="Recovered Today" value={data.summary.recovered_today} onClick={() => router.push("/self-heal/activity")} buttonLabel="VIEW ACTIVITY" />
            <KpiCard title="Warnings" value={data.summary.warning_problems} />
            <KpiCard
              title="Offline Cameras" value={data.summary.offline_cameras}
              onClick={() => router.push("/self-heal/camera-health")} buttonLabel="VIEW CAMERAS"
            />
            <KpiCard title="Degraded Cameras" value={data.summary.degraded_cameras} />
          </div>

          <div className="border border-border rounded-lg bg-panel divide-y divide-border">
            {SUBSYSTEM_ROWS.map((row) => (
              <div key={row.key} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-sm text-slate-300">{row.label}</span>
                <SubsystemDot value={String(data.subsystems[row.key])} />
              </div>
            ))}
            <div className="flex items-center justify-between px-4 py-2.5">
              <span className="text-sm text-slate-300">CAMERAS</span>
              <span className="text-xs font-medium text-slate-200">
                {data.cameras.online} / {data.cameras.total} ONLINE
                {data.cameras.degraded > 0 && <span className="text-high"> · {data.cameras.degraded} degraded</span>}
                {data.cameras.offline > 0 && <span className="text-slate-500"> · {data.cameras.offline} offline</span>}
              </span>
            </div>
            <div className="flex items-center justify-between px-4 py-2.5">
              <span className="text-sm text-slate-300">WEBSOCKET CLIENTS</span>
              <span className="text-xs font-medium text-slate-200">{data.subsystems.websocket_clients} connected</span>
            </div>
          </div>

          <div className="text-xs text-slate-500">Last checked {new Date(data.timestamp).toLocaleString()}</div>
        </>
      )}
    </div>
  );
}

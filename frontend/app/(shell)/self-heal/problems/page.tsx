"use client";
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket } from "@/lib/useLiveSocket";
import ErrorState from "@/components/ErrorState";
import EmptyState from "@/components/EmptyState";
import { SelfHealSeverityBadge, SelfHealStatusBadge } from "@/components/SelfHealBadges";

type ProblemEvent = {
  id: string; timestamp: string; component: string; camera_id: string | null; camera_code: string | null;
  error_type: string; severity: string; message: string; recovery_action: string;
  attempt: number; max_attempts: number; status: string; duration_seconds: number;
};

const COMPONENTS = ["camera", "database", "api", "websocket", "worker", "camera_catalog", "sentinel_grid"];
const SEVERITIES = ["critical", "warning"];

export default function SelfHealProblemsPage() {
  const router = useRouter();
  const [component, setComponent] = useState<string | null>(null);
  const [severity, setSeverity] = useState<string | null>(null);
  const path = `/api/self-heal/problems${component || severity ? "?" : ""}${[
    component ? `component=${component}` : "", severity ? `severity=${severity}` : "",
  ].filter(Boolean).join("&")}`;
  const { data, error, reload } = useApiData<ProblemEvent[]>(path, { pollMs: 6000 });

  // Live-updates the moment a new self-heal event lands (see self_heal/
  // engine.py's WebSocket broadcast) — reload rather than append, since a
  // problem's OWN latest event replaces its row rather than adding a new one.
  useLiveSocket((e) => {
    if (e.type === "self_heal_event") reload();
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Problems</h1>
          <p className="text-xs text-slate-400 mt-0.5">Everything Self-Heal currently considers still open — not yet recovered.</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <FilterPill active={!component && !severity} onClick={() => { setComponent(null); setSeverity(null); }}>All</FilterPill>
        {SEVERITIES.map((s) => (
          <FilterPill key={s} active={severity === s} onClick={() => setSeverity(severity === s ? null : s)}>{s}</FilterPill>
        ))}
        {COMPONENTS.map((c) => (
          <FilterPill key={c} active={component === c} onClick={() => setComponent(component === c ? null : c)}>{c.replace(/_/g, " ")}</FilterPill>
        ))}
      </div>

      {error ? (
        <ErrorState message={`Problems could not be loaded: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : data.length === 0 ? (
        <EmptyState title="No open problems" hint="Every tracked component's most recent recovery event was RECOVERED." />
      ) : (
        <div className="border border-border rounded-lg divide-y divide-border">
          {data.map((p) => (
            <button
              key={p.id}
              onClick={() => router.push(`/self-heal/problems/${p.id}`)}
              className="w-full text-left px-4 py-3 flex items-center justify-between gap-4 hover:bg-panel2 transition-colors duration-150"
            >
              <div className="min-w-0 flex items-center gap-3">
                <SelfHealSeverityBadge severity={p.severity} />
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-100 truncate">
                    {p.camera_code ? `Camera ${p.camera_code}` : p.component.replace(/_/g, " ")} · {p.error_type.replace(/_/g, " ")}
                  </div>
                  <div className="text-xs text-slate-400 truncate">{p.message}</div>
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <span className="text-xs text-slate-500">{new Date(p.timestamp).toLocaleTimeString()}</span>
                <SelfHealStatusBadge status={p.status} />
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function FilterPill({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`px-2.5 py-1 rounded-full text-xs capitalize border transition-colors duration-150 ${
        active ? "bg-accent/15 text-accent border-accent/40" : "border-border text-slate-400 hover:text-slate-200"
      }`}
    >
      {children}
    </button>
  );
}

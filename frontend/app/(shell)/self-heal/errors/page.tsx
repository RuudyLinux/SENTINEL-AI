"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Search } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";
import DataTable, { type Column } from "@/components/DataTable";
import { SelfHealSeverityBadge, SelfHealStatusBadge } from "@/components/SelfHealBadges";

type EventRow = {
  id: string; timestamp: string; component: string; camera_code: string | null;
  error_type: string; severity: string; recovery_action: string; status: string;
};

export default function ErrorLogsPage() {
  const [q, setQ] = useState("");
  const qs = new URLSearchParams({ limit: "100", ...(q && { q }) }).toString();
  const { data, error, reload } = useApiData<{ events: EventRow[]; total: number }>(`/api/self-heal/events?${qs}`, { pollMs: 10000 });
  const router = useRouter();

  const columns: Column<EventRow>[] = [
    { key: "timestamp", label: "Time", render: (r) => <span className="text-xs text-slate-400">{new Date(r.timestamp).toLocaleTimeString()}</span> },
    { key: "component", label: "Component", render: (r) => <span className="capitalize">{r.camera_code ? `${r.component} · ${r.camera_code}` : r.component.replace(/_/g, " ")}</span> },
    { key: "error_type", label: "Error", render: (r) => <span className="text-xs">{r.error_type.replace(/_/g, " ")}</span> },
    { key: "severity", label: "Severity", render: (r) => <SelfHealSeverityBadge severity={r.severity} /> },
    { key: "recovery_action", label: "Recovery", render: (r) => <span className="text-xs text-slate-400">{r.recovery_action || "—"}</span> },
    { key: "status", label: "Status", render: (r) => <SelfHealStatusBadge status={r.status} /> },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Error Logs</h1>
          <p className="text-xs text-slate-400 mt-0.5">Every recovery event Self-Heal has recorded, searchable.</p>
        </div>
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search message…"
            className="bg-panel border border-border rounded pl-8 pr-3 py-1.5 text-xs text-slate-200 w-56 focus:outline-none focus:border-accent"
          />
        </div>
      </div>

      {error ? (
        <ErrorState message={`Error log could not be loaded: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <>
          <DataTable
            columns={columns}
            rows={data.events}
            onRowClick={(r) => router.push(`/self-heal/problems/${r.id}`)}
            emptyTitle="No matching events"
            emptyHint={q ? "Try a different search term." : "No recovery events have been recorded yet."}
          />
          <div className="text-xs text-slate-500">{data.total} total</div>
        </>
      )}
    </div>
  );
}

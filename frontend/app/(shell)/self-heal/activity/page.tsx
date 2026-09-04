"use client";
import { useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket } from "@/lib/useLiveSocket";
import ErrorState from "@/components/ErrorState";
import EmptyState from "@/components/EmptyState";

type EventRow = {
  id: string; timestamp: string; component: string; camera_code: string | null;
  error_type: string; severity: string; recovery_action: string; status: string; duration_seconds: number;
};

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 1000) return "just now";
  if (ms < 60000) return `${Math.round(ms / 1000)}s ago`;
  if (ms < 3600000) return `${Math.round(ms / 60000)}m ago`;
  return `${Math.round(ms / 3600000)}h ago`;
}

const COMPONENTS = ["camera", "database", "api", "websocket", "worker", "camera_catalog", "sentinel_grid"];

export default function RecoveryActivityPage() {
  const [component, setComponent] = useState("");
  const [status, setStatus] = useState("");
  const qs = new URLSearchParams({ limit: "50", ...(component && { component }), ...(status && { status }) }).toString();
  const { data, error, reload } = useApiData<{ events: EventRow[]; total: number }>(`/api/self-heal/events?${qs}`, { pollMs: 6000 });

  useLiveSocket((e) => {
    if (e.type === "self_heal_event") reload();
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Recovery Activity</h1>
        <p className="text-xs text-slate-400 mt-0.5">Every recovery attempt Self-Heal has recorded, newest first.</p>
      </div>

      <div className="flex flex-wrap gap-2">
        <select value={component} onChange={(e) => setComponent(e.target.value)} className="bg-panel border border-border rounded px-2 py-1.5 text-xs text-slate-300">
          <option value="">All components</option>
          {COMPONENTS.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="bg-panel border border-border rounded px-2 py-1.5 text-xs text-slate-300">
          <option value="">All statuses</option>
          <option value="RECOVERED">Recovered</option>
          <option value="FAILED">Failed</option>
          <option value="CONFIG_REQUIRED">Config required</option>
        </select>
      </div>

      {error ? (
        <ErrorState message={`Recovery activity could not be loaded: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : data.events.length === 0 ? (
        <EmptyState title="No recovery activity yet" hint="Recovery events appear here as Self-Heal actually retries/reconnects something." />
      ) : (
        <div className="border border-border rounded-lg divide-y divide-border">
          {data.events.map((e) => {
            const ok = e.status === "RECOVERED";
            return (
              <div key={e.id} className="px-4 py-3 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3 min-w-0">
                  {ok ? <CheckCircle2 size={16} className="text-ok shrink-0" /> : <XCircle size={16} className="text-critical shrink-0" />}
                  <div className="min-w-0">
                    <div className="text-sm text-slate-100 truncate">
                      {e.camera_code ? `Camera ${e.camera_code}` : e.component.replace(/_/g, " ")} {ok ? "recovered" : "recovery failed"}
                    </div>
                    <div className="text-xs text-slate-400 truncate">{e.recovery_action || e.error_type.replace(/_/g, " ")}</div>
                  </div>
                </div>
                <span className="text-xs text-slate-500 shrink-0">{timeAgo(e.timestamp)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

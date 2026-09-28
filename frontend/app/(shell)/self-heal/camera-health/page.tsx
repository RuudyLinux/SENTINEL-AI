"use client";
import { useState } from "react";
import { X } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";
import DataTable, { type Column } from "@/components/DataTable";
import StatusDot from "@/components/StatusDot";
import { api, ApiError } from "@/lib/api";
import { aiState } from "@/lib/cameraState";
import { SelfHealStatusBadge } from "@/components/SelfHealBadges";

type Camera = {
  id: string; camera_code: string; name: string; status: string; fps: number; latency_ms: number;
  error_count: number; last_frame_at: string | null; grid_state: string | null;
  reconnect_count: number | null; last_error: string | null; ai_person: boolean; ai_vehicle: boolean;
};

type Diagnostics = Record<string, unknown>;
type SelfHealEvent = { id: string; timestamp: string; error_type: string; recovery_action: string; status: string };

export default function CameraHealthPage() {
  const { data: cameras, error, reload } = useApiData<Camera[]>("/api/cameras", { pollMs: 5000 });
  const [selected, setSelected] = useState<Camera | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [history, setHistory] = useState<SelfHealEvent[] | null>(null);

  async function openCamera(c: Camera) {
    setSelected(c);
    setDiagnostics(null);
    setDiagError(null);
    setHistory(null);
    try {
      const diag = await api.get<Diagnostics>(`/api/cameras/${c.id}/diagnostics`);
      setDiagnostics(diag);
    } catch (err) {
      setDiagError(err instanceof ApiError ? err.message : "Diagnostics unavailable");
    }
    try {
      const events = await api.get<{ events: SelfHealEvent[] }>(`/api/self-heal/events?camera_id=${c.id}&limit=10`);
      setHistory(events.events);
    } catch {
      setHistory([]);
    }
  }

  const columns: Column<Camera>[] = [
    { key: "camera_code", label: "Camera", render: (c) => <span className="font-medium text-slate-100">{c.camera_code}</span> },
    { key: "status", label: "Connection", render: (c) => <StatusDot status={c.status} /> },
    { key: "grid_state", label: "Stream", render: (c) => <span className="text-xs text-slate-400">{c.grid_state || "—"}</span> },
    { key: "ai", label: "AI", render: (c) => <span className="text-xs">{aiState(c)}</span> },
    { key: "fps", label: "FPS", render: (c) => <span className="text-xs">{c.fps ? c.fps.toFixed(0) : "—"}</span> },
    { key: "latency_ms", label: "Latency", render: (c) => <span className="text-xs">{c.latency_ms ? `${c.latency_ms.toFixed(0)}ms` : "—"}</span> },
    { key: "error_count", label: "Errors", render: (c) => <span className={`text-xs ${c.error_count > 0 ? "text-high" : "text-slate-400"}`}>{c.error_count}</span> },
    { key: "last_error", label: "Last Error", render: (c) => <span className="text-xs text-slate-500 max-w-[16rem] truncate block">{c.last_error || "—"}</span> },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Camera Health</h1>
        <p className="text-xs text-slate-400 mt-0.5">Live per-camera connection, stream and AI status. Click a camera for detail.</p>
      </div>

      {error ? (
        <ErrorState message={`Camera health could not be loaded: ${error}`} onRetry={reload} />
      ) : !cameras ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <DataTable columns={columns} rows={cameras} onRowClick={openCamera} emptyTitle="No cameras registered" />
      )}

      {selected && (
        <div className="fixed inset-0 z-50 flex justify-end animate-fade-in" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-black/50" onClick={() => setSelected(null)} />
          <div className="relative w-full max-w-md bg-panel border-l border-border h-full overflow-y-auto p-5 space-y-5 animate-slide-up">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">{selected.camera_code}</h2>
              <button onClick={() => setSelected(null)} className="text-slate-400 hover:text-slate-100"><X size={18} /></button>
            </div>

            {diagError ? (
              <div className="text-xs text-slate-400">Detailed diagnostics require Administrator/Control Room Operator — showing basic status only.</div>
            ) : !diagnostics ? (
              <div className="text-xs text-slate-500">Loading diagnostics…</div>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                {[
                  ["Worker", String(diagnostics.worker_task_state ?? "—")],
                  ["Grid state", String(diagnostics.grid_state ?? "—")],
                  ["Frames read", String(diagnostics.frames_read ?? "—")],
                  ["Frames processed", String(diagnostics.frames_processed ?? "—")],
                  ["Read failures", String(diagnostics.read_failures ?? "—")],
                  ["Reconnects", String(diagnostics.reconnects ?? "—")],
                  ["Recovered errors", String(diagnostics.recovered_errors ?? "—")],
                  ["Inference (ms)", diagnostics.inference_ms_ema != null ? Number(diagnostics.inference_ms_ema).toFixed(1) : "—"],
                ].map(([label, value]) => (
                  <div key={label} className="border border-border rounded px-2.5 py-2">
                    <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
                    <div className="text-sm text-slate-100 mt-0.5">{value}</div>
                  </div>
                ))}
              </div>
            )}

            <div>
              <h3 className="text-xs font-medium text-slate-300 uppercase tracking-wide mb-2">Recovery History</h3>
              {history === null ? (
                <div className="text-xs text-slate-500">Loading…</div>
              ) : history.length === 0 ? (
                <div className="text-xs text-slate-500">No recovery events recorded for this camera.</div>
              ) : (
                <div className="border border-border rounded-lg divide-y divide-border">
                  {history.map((h) => (
                    <div key={h.id} className="px-3 py-2 flex items-center justify-between text-xs">
                      <span className="text-slate-300">{h.error_type.replace(/_/g, " ")} · {h.recovery_action || "—"}</span>
                      <div className="flex items-center gap-2 shrink-0">
                        <span className="text-slate-500">{new Date(h.timestamp).toLocaleTimeString()}</span>
                        <SelfHealStatusBadge status={h.status} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

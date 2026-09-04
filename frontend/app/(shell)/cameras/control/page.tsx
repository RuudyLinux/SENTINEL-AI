"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Wifi, WifiOff, Play, BrainCircuit, RotateCcw, Square, MoreVertical,
  CheckCircle2, XCircle, SkipForward, HeartPulse, Loader2,
} from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket } from "@/lib/useLiveSocket";
import { api, ApiError, getStoredUser } from "@/lib/api";
import ErrorState from "@/components/ErrorState";
import StatusDot from "@/components/StatusDot";

type Camera = {
  id: string; camera_code: string; name: string; location: string; status: string;
  fps: number; latency_ms: number; grid_state: string | null; last_error: string | null;
};

type BulkAction = "connect" | "start" | "start_ai" | "restart" | "stop" | "disconnect";

const BULK_BUTTONS: { action: BulkAction; label: string; icon: typeof Wifi }[] = [
  { action: "connect", label: "Connect", icon: Wifi },
  { action: "start", label: "Start", icon: Play },
  { action: "start_ai", label: "Start AI", icon: BrainCircuit },
  { action: "restart", label: "Restart", icon: RotateCcw },
  { action: "stop", label: "Stop", icon: Square },
  { action: "disconnect", label: "Disconnect", icon: WifiOff },
];

const DISRUPTIVE = new Set<BulkAction>(["restart", "disconnect", "stop"]);

const DISRUPTIVE_COPY: Record<string, string> = {
  restart: "This will temporarily interrupt active streams.",
  disconnect: "This will fully stop the camera worker and end the stream.",
  stop: "This will stop AI processing on the selected camera(s); the stream stays connected.",
};

function aiState(c: Camera): string {
  if (c.grid_state === "PROCESSING") return "AI RUNNING";
  if (c.grid_state === "CONNECTED") return "AI STOPPED";
  if (c.grid_state === "ERROR") return "AI ERROR";
  if (c.grid_state === "RECONNECTING" || c.grid_state === "CONNECTING") return "AI STARTING";
  return "—";
}

type ProgressState = {
  opId: string; action: BulkAction; total: number; completed: number;
  results: { camera_id: string; camera_code: string | null; ok: boolean; skipped: boolean; detail: string }[];
} | null;

type CompleteState = { opId: string; total: number; successful: number; failed: number; skipped: number } | null;

export default function CameraControlCenterPage() {
  const { data: cameras, error, reload } = useApiData<Camera[]>("/api/cameras", { pollMs: 5000 });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmAction, setConfirmAction] = useState<{ action: BulkAction; ids: string[] | null } | null>(null);
  const [progress, setProgress] = useState<ProgressState>(null);
  const [complete, setComplete] = useState<CompleteState>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const user = useMemo(() => getStoredUser(), []);
  const canControl = user?.role === "Administrator" || user?.role === "Control Room Operator";

  useLiveSocket((e) => {
    if (e.type === "bulk_progress") {
      setProgress((prev) => {
        if (!prev || prev.opId !== e.data.op_id) {
          return { opId: e.data.op_id, action: e.data.action, total: e.data.total, completed: e.data.completed, results: [e.data.result] };
        }
        return { ...prev, completed: e.data.completed, results: [...prev.results, e.data.result] };
      });
    }
    if (e.type === "bulk_complete") {
      setComplete({ opId: e.data.op_id, total: e.data.total, successful: e.data.successful, failed: e.data.failed, skipped: e.data.skipped });
      reload();
    }
  });

  function toggleAll() {
    if (!cameras) return;
    setSelected(selected.size === cameras.length ? new Set() : new Set(cameras.map((c) => c.id)));
  }
  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function runBulk(action: BulkAction, ids: string[] | null) {
    setActionError(null);
    setComplete(null);
    setProgress({ opId: "", action, total: ids ? ids.length : (cameras?.length ?? 0), completed: 0, results: [] });
    try {
      const res = await api.post<{ op_id: string; successful: number; failed: number; skipped: number; total: number }>(
        "/api/cameras/bulk", { action, camera_ids: ids }
      );
      setComplete({ opId: res.op_id, total: res.total, successful: res.successful, failed: res.failed, skipped: res.skipped });
      setProgress(null);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Bulk operation failed");
      setProgress(null);
    }
  }

  function requestBulk(action: BulkAction, scope: "selected" | "all") {
    const ids = scope === "selected" ? Array.from(selected) : null;
    if (scope === "selected" && ids && ids.length === 0) return;
    if (DISRUPTIVE.has(action)) {
      setConfirmAction({ action, ids });
    } else {
      runBulk(action, ids);
    }
  }

  async function runSingle(action: BulkAction, camera: Camera) {
    setActionError(null);
    try {
      await api.post("/api/cameras/bulk", { action, camera_ids: [camera.id] });
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `${action} failed for ${camera.camera_code}`);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Camera Control Center</h1>
          <p className="text-xs text-slate-400 mt-0.5">Bulk and individual control over every registered camera.</p>
        </div>
      </div>

      {!canControl && (
        <div className="text-xs text-high border border-high/30 bg-high/5 rounded-lg px-3 py-2">
          Your role ({user?.role || "unknown"}) has view-only access here — control actions require Administrator or Control Room Operator.
        </div>
      )}

      {actionError && (
        <div className="text-xs text-critical border border-critical/30 bg-critical/5 rounded-lg px-3 py-2">{actionError}</div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {BULK_BUTTONS.map(({ action, label, icon: Icon }) => (
          <button
            key={action}
            disabled={!canControl || !!progress}
            onClick={() => requestBulk(action, selected.size > 0 ? "selected" : "all")}
            className="flex items-center gap-1.5 text-xs font-medium border border-border rounded px-3 py-1.5 hover:border-accent hover:text-accent transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none"
          >
            <Icon size={13} strokeWidth={2.25} />
            {label} {selected.size > 0 ? "Selected" : "All"}
          </button>
        ))}
      </div>

      {progress && (
        <div className="border border-border rounded-lg bg-panel p-4 space-y-2 animate-fade-in">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium text-slate-100">{progress.action.replace(/_/g, " ").toUpperCase()}ING CAMERAS</span>
            <span className="text-slate-400">{progress.completed} / {progress.total} completed</span>
          </div>
          <div className="h-2 bg-panel2 rounded-full overflow-hidden">
            <div
              className="h-full bg-accent transition-[width] duration-300"
              style={{ width: `${progress.total ? (progress.completed / progress.total) * 100 : 0}%` }}
            />
          </div>
          <div className="max-h-32 overflow-y-auto space-y-1 text-xs">
            {progress.results.map((r, i) => (
              <div key={i} className="flex items-center gap-2">
                {r.skipped ? <SkipForward size={12} className="text-slate-500" /> : r.ok ? <CheckCircle2 size={12} className="text-ok" /> : <XCircle size={12} className="text-critical" />}
                <span className="text-slate-400">{r.camera_code || r.camera_id} — {r.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {complete && (
        <div className="border border-border rounded-lg bg-panel p-4 space-y-2 animate-fade-in">
          <div className="text-sm font-medium text-slate-100">OPERATION COMPLETE</div>
          <div className="flex gap-4 text-xs text-slate-300">
            <span className="text-ok">Successful: {complete.successful}</span>
            <span className="text-critical">Failed: {complete.failed}</span>
            <span className="text-slate-400">Skipped: {complete.skipped}</span>
            <span className="text-slate-500">Total: {complete.total}</span>
          </div>
        </div>
      )}

      {error ? (
        <ErrorState message={`Cameras could not be loaded: ${error}`} onRetry={reload} />
      ) : !cameras ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <div className="overflow-x-auto border border-border rounded-lg">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-panel2 text-slate-400 text-xs uppercase tracking-wide">
                <th className="px-3 py-2 text-left">
                  <input type="checkbox" checked={cameras.length > 0 && selected.size === cameras.length} onChange={toggleAll} aria-label="Select all cameras" />
                </th>
                {["Camera", "Location", "Connection", "AI", "FPS", "Latency", "Last Error", "Actions"].map((h) => (
                  <th key={h} className="px-3 py-2 text-left font-medium whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cameras.map((c) => (
                <tr key={c.id} className="border-t border-border hover:bg-panel2/50 transition-colors duration-150">
                  <td className="px-3 py-2"><input type="checkbox" checked={selected.has(c.id)} onChange={() => toggleOne(c.id)} aria-label={`Select ${c.camera_code}`} /></td>
                  <td className="px-3 py-2 font-medium text-slate-100 whitespace-nowrap">{c.camera_code}</td>
                  <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{c.location || "—"}</td>
                  <td className="px-3 py-2 whitespace-nowrap"><StatusDot status={c.status} /></td>
                  <td className="px-3 py-2 whitespace-nowrap text-xs">{aiState(c)}</td>
                  <td className="px-3 py-2 whitespace-nowrap text-xs">{c.fps ? c.fps.toFixed(0) : "—"}</td>
                  <td className="px-3 py-2 whitespace-nowrap text-xs">{c.latency_ms ? `${c.latency_ms.toFixed(0)}ms` : "—"}</td>
                  <td className="px-3 py-2 text-xs text-slate-500 max-w-[14rem] truncate">{c.last_error || "—"}</td>
                  <td className="px-3 py-2 relative">
                    <RowActionsMenu
                      camera={c}
                      canControl={canControl}
                      onAction={async (action) => {
                        if (DISRUPTIVE.has(action)) {
                          setConfirmAction({ action, ids: [c.id] });
                        } else {
                          await runSingle(action, c);
                        }
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {confirmAction && (
        <div className="fixed inset-0 z-50 flex items-center justify-center animate-fade-in" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-black/50" onClick={() => setConfirmAction(null)} />
          <div className="relative bg-panel border border-border rounded-lg p-5 w-full max-w-sm space-y-3 animate-scale-in">
            <h2 className="text-sm font-semibold text-slate-100">
              {confirmAction.action.replace(/_/g, " ")} {confirmAction.ids ? confirmAction.ids.length : cameras?.length ?? 0} camera(s)?
            </h2>
            <p className="text-xs text-slate-400">{DISRUPTIVE_COPY[confirmAction.action]}</p>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setConfirmAction(null)} className="text-xs px-3 py-1.5 rounded border border-border text-slate-300 hover:bg-panel2">Cancel</button>
              <button
                onClick={() => { runBulk(confirmAction.action, confirmAction.ids); setConfirmAction(null); }}
                className="text-xs px-3 py-1.5 rounded bg-critical/15 text-critical border border-critical/30 hover:bg-critical/25"
              >
                {confirmAction.action.replace(/_/g, " ")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Per-camera "..." action menu — owns its own open/busy state so a double
 * click can't fire the same action twice (menu closes as soon as the
 * request starts, button disabled until it resolves), and closes itself on
 * an outside click (audit finding: previously stayed open until another
 * menu item was clicked). */
function RowActionsMenu({
  camera, canControl, onAction,
}: { camera: Camera; canControl: boolean; onAction: (action: BulkAction) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  async function handle(action: BulkAction) {
    setOpen(false);
    setBusy(true);
    try {
      await onAction(action);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div ref={containerRef} className="relative inline-block">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={!canControl || busy}
        className="p-1 rounded hover:bg-panel2 disabled:opacity-40"
        aria-label={`Actions for ${camera.camera_code}`}
      >
        {busy ? <Loader2 size={15} className="animate-spin" /> : <MoreVertical size={15} />}
      </button>
      {open && (
        <div className="absolute right-2 top-8 z-20 w-40 bg-panel border border-border rounded-md shadow-lg py-1 animate-dropdown-in">
          {BULK_BUTTONS.map(({ action, label }) => (
            <button
              key={action}
              onClick={() => handle(action)}
              className="w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-panel2 hover:text-slate-100"
            >
              {label}
            </button>
          ))}
          <a
            href="/self-heal/camera-health"
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-slate-300 hover:bg-panel2 hover:text-slate-100 border-t border-border"
          >
            <HeartPulse size={12} /> View Health
          </a>
        </div>
      )}
    </div>
  );
}

"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, getStoredUser, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import StatusDot from "@/components/StatusDot";
import ErrorState from "@/components/ErrorState";

// Mirrors the backend's require_roles("Administrator", "Control Room Operator")
// on catalog sync / create / start / stop / restart (see routers/cameras.py) —
// the backend is the actual enforcement; this just keeps an Auditor/
// Investigator/Supervisor from being shown buttons that would 403.
const CAN_MANAGE_CAMERAS = ["Administrator", "Control Room Operator"];

export default function CamerasPage() {
  const router = useRouter();
  const { data: cameras, error, reload } = useApiData<any[]>("/api/cameras", { pollMs: 5000 });
  const [actionError, setActionError] = useState<string | null>(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [gridSyncBusy, setGridSyncBusy] = useState(false);
  const [gridSyncResult, setGridSyncResult] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [canManage, setCanManage] = useState(false);
  const [groupFilter, setGroupFilter] = useState<string>("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ name: "", location: "", camera_group: "", ai_person: true, ai_vehicle: true, ai_anpr: true });
  const [editBusy, setEditBusy] = useState(false);

  useEffect(() => {
    const user = getStoredUser();
    setCanManage(!!user && CAN_MANAGE_CAMERAS.includes(user.role));
  }, []);

  function toggle(id: string, e?: React.MouseEvent) {
    e?.stopPropagation();
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function startEdit(c: any, e: React.MouseEvent) {
    e.stopPropagation();
    setEditingId(c.id);
    setEditForm({
      name: c.name, location: c.location, camera_group: c.camera_group || "",
      ai_person: c.ai_person, ai_vehicle: c.ai_vehicle, ai_anpr: c.ai_anpr,
    });
  }

  async function saveEdit(id: string) {
    setEditBusy(true);
    setActionError(null);
    try {
      await api.patch(`/api/cameras/${id}`, editForm);
      setEditingId(null);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not update camera");
    } finally {
      setEditBusy(false);
    }
  }

  async function restart(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    setActionError(null);
    try {
      await api.post(`/api/cameras/${id}/restart`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Restart request failed");
    }
  }

  async function syncCatalog() {
    setSyncBusy(true);
    setSyncResult(null);
    setActionError(null);
    try {
      const res = await api.post<any>("/api/cameras/catalog/sync");
      setSyncResult(
        `Registry updated: ${res.created} created, ${res.updated} updated, ${res.marked_stale} marked stale ` +
        `(${res.total_in_catalogue} in catalogue, ${res.skipped_invalid} skipped). No cameras were connected — start them below.`
      );
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Catalogue sync failed");
    } finally {
      setSyncBusy(false);
    }
  }

  async function syncSentinelGrid() {
    setGridSyncBusy(true);
    setGridSyncResult(null);
    setActionError(null);
    try {
      const res = await api.post<any>("/api/cameras/sentinel-grid/sync");
      setGridSyncResult(
        `Sentinel Grid registry updated: ${res.created} created, ${res.updated} updated, ${res.marked_stale} marked stale ` +
        `(${res.total_in_grid} in grid, ${res.skipped_invalid} skipped). No cameras were connected — start them below.`
      );
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Sentinel Grid sync failed");
    } finally {
      setGridSyncBusy(false);
    }
  }

  // Catalogue sync only REGISTERS cameras — CONNECTING (starting AI
  // processing) is a separate, explicit step the operator takes here.
  async function bulkAction(action: "start" | "stop") {
    setBulkBusy(true);
    setActionError(null);
    try {
      for (const id of selected) {
        await api.post(`/api/cameras/${id}/${action}`);
      }
      setSelected(new Set());
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Bulk ${action} failed`);
    } finally {
      setBulkBusy(false);
    }
  }

  const columns: Column<any>[] = [
    ...(canManage ? [{
      key: "select", label: "",
      render: (c: any) => (
        <input
          type="checkbox"
          checked={selected.has(c.id)}
          onChange={() => {}}
          onClick={(e: React.MouseEvent) => toggle(c.id, e)}
        />
      ),
    }] : []),
    { key: "camera_code", label: "Camera ID" },
    { key: "name", label: "Name" },
    { key: "location", label: "Location" },
    { key: "camera_group", label: "Group", render: (c) => c.camera_group ? <span className="text-xs text-slate-400">{c.camera_group}</span> : <span className="text-xs text-slate-600">—</span> },
    {
      key: "status", label: "Status",
      render: (c) => (
        <div className="flex items-center gap-1.5">
          <StatusDot status={c.status} />
          {/* Catalogue availability (row exists) is a different fact from live stream
              connectivity — a registered-but-never-started camera must not read as
              "online" via status alone, so it's called out explicitly here rather
              than relying on "offline" (also shown for a camera that was connected
              and then dropped) to imply "never connected." */}
          {c.external_catalog_id && !c.last_frame_at && c.status === "offline" && (
            <span className="text-[10px] text-slate-500 border border-border rounded px-1" title="Discovered from a camera catalogue; never connected/started">
              REGISTERED
            </span>
          )}
        </div>
      ),
    },
    { key: "fps", label: "FPS", render: (c) => c.fps?.toFixed(1) ?? "—" },
    { key: "resolution", label: "Resolution" },
    {
      key: "catalog", label: "Catalogue",
      render: (c) =>
        c.external_catalog_id ? (
          <span className="text-xs text-slate-400">
            {c.external_catalog_id}
            {c.catalog_codec && ` · ${c.catalog_codec}`}
            {c.catalog_stale && <span className="ml-1 badge bg-high/15 text-high border border-high/30">STALE</span>}
          </span>
        ) : (
          <span className="text-xs text-slate-600">manual</span>
        ),
    },
    { key: "error_count", label: "Errors" },
    ...(canManage ? [{
      key: "actions", label: "Actions",
      render: (c: any) => (
        <div className="flex gap-2">
          <button onClick={(e: React.MouseEvent) => startEdit(c, e)} className="text-xs text-accent hover:underline">Edit</button>
          <button onClick={(e: React.MouseEvent) => restart(c.id, e)} className="text-xs text-accent hover:underline">Restart</button>
        </div>
      ),
    }] : []),
  ];

  const allCameras = cameras || [];
  const groups = Array.from(new Set(allCameras.map((c: any) => c.camera_group).filter(Boolean))) as string[];
  const visibleCameras = groupFilter ? allCameras.filter((c: any) => c.camera_group === groupFilter) : allCameras;
  const editingCamera = editingId ? allCameras.find((c: any) => c.id === editingId) : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Camera Management</h1>
        {canManage && (
          <div className="flex gap-2">
            <button
              onClick={syncCatalog}
              disabled={syncBusy}
              className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent disabled:opacity-50"
            >
              {syncBusy ? "SYNCING…" : "SYNC CAMERA CATALOGUE"}
            </button>
            <button
              onClick={syncSentinelGrid}
              disabled={gridSyncBusy}
              className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent disabled:opacity-50"
            >
              {gridSyncBusy ? "SYNCING…" : "SYNC SENTINEL GRID"}
            </button>
            <Link href="/cameras/add" className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">ADD CAMERA</Link>
          </div>
        )}
      </div>

      {groups.length > 0 && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-slate-400">Group:</span>
          <select value={groupFilter} onChange={(e) => setGroupFilter(e.target.value)} className="bg-panel2 border border-border rounded px-2 py-1">
            <option value="">All groups</option>
            {groups.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </div>
      )}

      {editingCamera && (
        <div className="bg-panel border border-border rounded-lg p-4 space-y-3 max-w-md">
          <div className="text-sm font-medium">Edit {editingCamera.camera_code}</div>
          <label className="block text-xs text-slate-400">Name
            <input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} className="input" />
          </label>
          <label className="block text-xs text-slate-400">Location
            <input value={editForm.location} onChange={(e) => setEditForm({ ...editForm, location: e.target.value })} className="input" />
          </label>
          <label className="block text-xs text-slate-400">Group
            <input value={editForm.camera_group} onChange={(e) => setEditForm({ ...editForm, camera_group: e.target.value })} placeholder="North Zone" className="input" />
          </label>
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2"><input type="checkbox" checked={editForm.ai_person} onChange={(e) => setEditForm({ ...editForm, ai_person: e.target.checked })} /> Person</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={editForm.ai_vehicle} onChange={(e) => setEditForm({ ...editForm, ai_vehicle: e.target.checked })} /> Vehicle</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={editForm.ai_anpr} onChange={(e) => setEditForm({ ...editForm, ai_anpr: e.target.checked })} /> ANPR</label>
          </div>
          <div className="flex gap-2">
            <button disabled={editBusy} onClick={() => saveEdit(editingCamera.id)} className="text-xs bg-accent text-ink font-medium rounded px-4 py-2 disabled:opacity-50">
              {editBusy ? "Saving..." : "SAVE"}
            </button>
            <button onClick={() => setEditingId(null)} className="text-xs border border-border rounded px-4 py-2">CANCEL</button>
          </div>
          <style jsx global>{`.input { width: 100%; background: #161f2c; border: 1px solid #22303f; border-radius: 6px; padding: 6px 10px; font-size: 13px; margin-top: 4px; }`}</style>
        </div>
      )}

      {syncResult && <div className="text-xs text-ok bg-ok/10 border border-ok/30 rounded px-3 py-2">{syncResult}</div>}
      {gridSyncResult && <div className="text-xs text-ok bg-ok/10 border border-ok/30 rounded px-3 py-2">{gridSyncResult}</div>}
      {actionError && <div className="text-xs text-critical">{actionError}</div>}

      {canManage && selected.size > 0 && (
        <div className="flex items-center gap-3 text-xs bg-panel2 border border-border rounded px-3 py-2">
          <span>{selected.size} selected</span>
          <button disabled={bulkBusy} onClick={() => bulkAction("start")} className="text-accent hover:underline disabled:opacity-50">Start selected</button>
          <button disabled={bulkBusy} onClick={() => bulkAction("stop")} className="text-critical hover:underline disabled:opacity-50">Stop selected</button>
          <button onClick={() => setSelected(new Set())} className="text-slate-400 hover:underline ml-auto">Clear</button>
        </div>
      )}

      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable
          columns={columns}
          rows={visibleCameras}
          onRowClick={(c) => router.push(`/live/${c.id}`)}
          emptyTitle="No cameras registered"
          emptyHint="Sync the official camera catalogue, or add a webcam / test video file to start the real detection pipeline."
        />
      )}
    </div>
  );
}

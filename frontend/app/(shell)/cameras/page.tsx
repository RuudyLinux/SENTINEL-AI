"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, getStoredUser, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import { hasLocation } from "@/lib/geo";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";
import ConnectionBadge, { AiBadge, deriveConnectionState } from "@/components/ConnectionBadge";
import KpiCard from "@/components/KpiCard";

// Mirrors the backend's role check on camera routes; only hides buttons that
// would return 403.
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
  const [isAdmin, setIsAdmin] = useState(false);
  const [groupFilter, setGroupFilter] = useState<string>("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ name: "", location: "", camera_group: "", lat: "", lng: "", ai_person: true, ai_vehicle: true, ai_anpr: true });
  const [editBusy, setEditBusy] = useState(false);
  const [rowBusyId, setRowBusyId] = useState<string | null>(null);

  useEffect(() => {
    const user = getStoredUser();
    setCanManage(!!user && CAN_MANAGE_CAMERAS.includes(user.role));
    setIsAdmin(user?.role === "Administrator");
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
      // 0,0 is how the backend stores "unknown"; shown blank, not as a place.
      lat: hasLocation(c) ? String(c.lat) : "", lng: hasLocation(c) ? String(c.lng) : "",
      ai_person: c.ai_person, ai_vehicle: c.ai_vehicle, ai_anpr: c.ai_anpr,
    });
  }

  async function saveEdit(id: string) {
    setEditBusy(true);
    setActionError(null);
    try {
      const blankLat = editForm.lat.trim() === "";
      const blankLng = editForm.lng.trim() === "";
      const lat = blankLat ? 0 : Number(editForm.lat);
      const lng = blankLng ? 0 : Number(editForm.lng);
      if (blankLat !== blankLng || !Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) {
        setActionError("Enter both latitude and longitude, or leave both blank if the location is unknown");
        return;
      }
      await api.patch(`/api/cameras/${id}`, { ...editForm, lat, lng });
      setEditingId(null);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not update camera");
    } finally {
      setEditBusy(false);
    }
  }

  // Retiring keeps the history and hides the camera from active lists.
  async function retire(c: any, e: React.MouseEvent) {
    e.stopPropagation();
    if (!window.confirm(`Retire ${c.camera_code}? It stops, disappears from active camera lists and can no longer be connected. Its history is kept.`)) return;
    setActionError(null);
    try {
      await api.post(`/api/cameras/${c.id}/retire`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not retire camera");
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

  // Sync only registers cameras; connecting is a separate step. /start and
  // /stop connect and disconnect the stream.
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

  // Start/Stop AI toggles ai_person/ai_vehicle via PATCH; the stream stays
  // connected. ai_anpr is left as configured (it needs vehicle detections).
  async function bulkAiAction(on: boolean) {
    setBulkBusy(true);
    setActionError(null);
    try {
      for (const id of selected) {
        await api.patch(`/api/cameras/${id}`, { ai_person: on, ai_vehicle: on });
      }
      setSelected(new Set());
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Bulk AI ${on ? "start" : "stop"} failed`);
    } finally {
      setBulkBusy(false);
    }
  }

  async function connectionAction(id: string, action: "start" | "stop", e: React.MouseEvent) {
    e.stopPropagation();
    setRowBusyId(id);
    setActionError(null);
    try {
      await api.post(`/api/cameras/${id}/${action}`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `${action === "start" ? "Connect" : "Disconnect"} failed`);
    } finally {
      setRowBusyId(null);
    }
  }

  async function aiAction(id: string, on: boolean, e: React.MouseEvent) {
    e.stopPropagation();
    setRowBusyId(id);
    setActionError(null);
    try {
      await api.patch(`/api/cameras/${id}`, { ai_person: on, ai_vehicle: on });
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `${on ? "Start AI" : "Stop AI"} failed`);
    } finally {
      setRowBusyId(null);
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
      // Connection lifecycle from grid_state, which distinguishes "never
      // connected" from "dropped".
      key: "connection", label: "Connection",
      render: (c) => <ConnectionBadge camera={c} />,
    },
    {
      // AI has its own column so a connected camera with AI off is explicit.
      key: "ai", label: "AI",
      render: (c) => <AiBadge camera={c} />,
    },
    {
      key: "last_frame_at", label: "Last Frame",
      render: (c) => c.last_frame_at ? <span className="text-xs text-slate-400">{new Date(c.last_frame_at).toLocaleTimeString()}</span> : <span className="text-xs text-slate-600">—</span>,
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
      render: (c: any) => {
        const busy = rowBusyId === c.id;
        const connected = deriveConnectionState(c) !== "REGISTERED" && deriveConnectionState(c) !== "DISCONNECTED";
        const aiOn = !!(c.ai_person || c.ai_vehicle);
        return (
          <div className="flex flex-wrap gap-2">
            {connected ? (
              <button disabled={busy} onClick={(e: React.MouseEvent) => connectionAction(c.id, "stop", e)} className="row-action text-xs text-critical hover:underline disabled:opacity-50">Disconnect</button>
            ) : (
              <button disabled={busy} onClick={(e: React.MouseEvent) => connectionAction(c.id, "start", e)} className="row-action text-xs text-accent hover:underline disabled:opacity-50">Connect</button>
            )}
            {/* Start/Stop AI only makes sense once a stream exists to process. */}
            {connected && (
              aiOn ? (
                <button disabled={busy} onClick={(e: React.MouseEvent) => aiAction(c.id, false, e)} className="row-action text-xs text-high hover:underline disabled:opacity-50">Stop AI</button>
              ) : (
                <button disabled={busy} onClick={(e: React.MouseEvent) => aiAction(c.id, true, e)} className="row-action text-xs text-accent hover:underline disabled:opacity-50">Start AI</button>
              )
            )}
            <button onClick={(e: React.MouseEvent) => startEdit(c, e)} className="row-action text-xs text-accent hover:underline">Edit</button>
            <button onClick={(e: React.MouseEvent) => restart(c.id, e)} className="row-action text-xs text-accent hover:underline">Restart</button>
            {isAdmin && (
              <button onClick={(e: React.MouseEvent) => retire(c, e)} className="row-action text-xs text-slate-400 hover:text-critical hover:underline">Retire</button>
            )}
          </div>
        );
      },
    }] : []),
  ];

  const allCameras = cameras || [];
  const groups = Array.from(new Set(allCameras.map((c: any) => c.camera_group).filter(Boolean))) as string[];
  const visibleCameras = groupFilter ? allCameras.filter((c: any) => c.camera_group === groupFilter) : allCameras;
  const editingCamera = editingId ? allCameras.find((c: any) => c.id === editingId) : null;

  // Auto-connect summary for grid cameras (external_catalog_id set).
  // Registered = Connected (incl. Processing) + Reconnecting + Disconnected.
  const gridCameras = allCameras.filter((c: any) => c.source_type === "sentinel_grid" && c.external_catalog_id);
  const gridRegistered = gridCameras.length;
  const gridConnected = gridCameras.filter((c: any) => ["CONNECTED", "PROCESSING"].includes(deriveConnectionState(c))).length;
  const gridProcessing = gridCameras.filter((c: any) => deriveConnectionState(c) === "PROCESSING").length;
  const gridReconnecting = gridCameras.filter((c: any) => deriveConnectionState(c) === "RECONNECTING").length;
  const gridDisconnected = Math.max(0, gridRegistered - gridConnected - gridReconnecting);
  // True once any worker has started (cameras only get grid_state then).
  const supervisorActive = gridCameras.some((c: any) => !!c.grid_state);
  const lastCatalogSync = gridCameras.reduce((latest: string | null, c: any) => {
    if (!c.catalog_synced_at) return latest;
    return !latest || c.catalog_synced_at > latest ? c.catalog_synced_at : latest;
  }, null as string | null);

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

      {gridRegistered > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between flex-wrap gap-1">
            <div className="text-xs uppercase tracking-wide text-slate-400">Real Sentinel Cameras: {gridRegistered}</div>
            <div className="text-[11px] text-slate-500">
              {supervisorActive ? <span className="text-ok">Supervisor: ACTIVE</span> : <span>Supervisor: not yet swept</span>}
              {lastCatalogSync && <> · Last catalogue sync: {new Date(lastCatalogSync).toLocaleString()}</>}
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 stagger-children">
            <KpiCard title="Registered" value={gridRegistered} />
            <KpiCard title="Connected" value={gridConnected} sub="live stream, AI on or off" />
            <KpiCard title="Processing" value={gridProcessing} sub="AI actively running" />
            <KpiCard title="Reconnecting" value={gridReconnecting} />
            <KpiCard title="Disconnected" value={gridDisconnected} sub="registered, not live" />
          </div>
        </div>
      )}

      {groups.length > 0 && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-slate-400">Group:</span>
          <select aria-label="Filter by camera group" value={groupFilter} onChange={(e) => setGroupFilter(e.target.value)} className="bg-panel2 border border-border rounded px-2 py-1">
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
          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs text-slate-400">Latitude (blank if unknown)
              <input type="number" step="0.0001" min={-90} max={90} value={editForm.lat} onChange={(e) => setEditForm({ ...editForm, lat: e.target.value })} className="input" />
            </label>
            <label className="block text-xs text-slate-400">Longitude (blank if unknown)
              <input type="number" step="0.0001" min={-180} max={180} value={editForm.lng} onChange={(e) => setEditForm({ ...editForm, lng: e.target.value })} className="input" />
            </label>
          </div>
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
        <div className="flex items-center gap-3 text-xs bg-panel2 border border-border rounded px-3 py-2 flex-wrap">
          <span>{selected.size} selected</span>
          <button disabled={bulkBusy} onClick={() => bulkAction("start")} className="text-accent hover:underline disabled:opacity-50">Connect selected</button>
          <button disabled={bulkBusy} onClick={() => bulkAction("stop")} className="text-critical hover:underline disabled:opacity-50">Disconnect selected</button>
          <button disabled={bulkBusy} onClick={() => bulkAiAction(true)} className="text-accent hover:underline disabled:opacity-50">Start AI selected</button>
          <button disabled={bulkBusy} onClick={() => bulkAiAction(false)} className="text-high hover:underline disabled:opacity-50">Stop AI selected</button>
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

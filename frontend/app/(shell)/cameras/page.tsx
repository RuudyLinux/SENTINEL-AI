"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import StatusDot from "@/components/StatusDot";
import ErrorState from "@/components/ErrorState";

export default function CamerasPage() {
  const router = useRouter();
  const { data: cameras, error, reload } = useApiData<any[]>("/api/cameras", { pollMs: 5000 });
  const [actionError, setActionError] = useState<string | null>(null);

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

  const columns: Column<any>[] = [
    { key: "camera_code", label: "Camera ID" },
    { key: "name", label: "Name" },
    { key: "location", label: "Location" },
    { key: "department", label: "Department" },
    { key: "status", label: "Status", render: (c) => <StatusDot status={c.status} /> },
    { key: "fps", label: "FPS", render: (c) => c.fps?.toFixed(1) ?? "—" },
    { key: "resolution", label: "Resolution" },
    { key: "error_count", label: "Errors" },
    {
      key: "actions", label: "Actions",
      render: (c) => (
        <button onClick={(e) => restart(c.id, e)} className="text-xs text-accent hover:underline">Restart</button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Camera Management</h1>
        <Link href="/cameras/add" className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">ADD CAMERA</Link>
      </div>
      {actionError && <div className="text-xs text-critical">{actionError}</div>}
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable
          columns={columns}
          rows={cameras || []}
          onRowClick={(c) => router.push(`/live/${c.id}`)}
          emptyTitle="No cameras registered"
          emptyHint="Onboard a webcam or upload a test video file to start the real detection pipeline."
        />
      )}
    </div>
  );
}

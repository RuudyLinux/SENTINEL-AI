"use client";
import { useRouter } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";

export default function EvidenceLibraryPage() {
  const router = useRouter();
  const { data: evidence, error, reload } = useApiData<any[]>("/api/evidence");

  const columns: Column<any>[] = [
    { key: "id", label: "Evidence ID" },
    { key: "evidence_type", label: "Type" },
    { key: "camera_id", label: "Source Camera", render: (e) => e.camera_id || "—" },
    { key: "incident_id", label: "Case", render: (e) => e.incident_id || "—" },
    { key: "verification_status", label: "Status" },
    { key: "created_at", label: "Captured", render: (e) => new Date(e.created_at).toLocaleString() },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Evidence Library</h1>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable
          columns={columns}
          rows={evidence || []}
          onRowClick={(e) => router.push(`/evidence/${e.id}`)}
          emptyTitle="No evidence yet"
          emptyHint="Evidence is attached automatically when a CRITICAL alert creates an incident."
        />
      )}
    </div>
  );
}

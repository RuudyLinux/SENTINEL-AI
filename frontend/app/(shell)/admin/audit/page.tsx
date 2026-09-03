"use client";
import { useState } from "react";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";

export default function AuditLogPage() {
  const [actorInput, setActorInput] = useState("");
  const [actor, setActor] = useState("");
  const path = actor ? `/api/audit?actor=${encodeURIComponent(actor)}` : "/api/audit";
  const { data: logs, error, reload } = useApiData<any[]>(path);

  const columns: Column<any>[] = [
    { key: "timestamp", label: "Timestamp", render: (l) => new Date(l.timestamp).toLocaleString() },
    { key: "username", label: "User" },
    { key: "action", label: "Action" },
    { key: "resource", label: "Resource" },
    { key: "result", label: "Result" },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Audit Logs</h1>
        <div className="flex items-center gap-2">
          <input value={actorInput} onChange={(e) => setActorInput(e.target.value)} placeholder="Filter by user..." className="bg-panel2 border border-border rounded px-3 py-1.5 text-sm" />
          <button onClick={() => setActor(actorInput)} className="text-xs border border-border rounded px-3 py-1.5">FILTER</button>
        </div>
      </div>
      {error ? (
        <ErrorState message={`Audit trail could not be loaded: ${error} (this endpoint requires the Administrator or Auditor role — an empty table on failure would misrepresent whether activity actually occurred)`} onRetry={reload} />
      ) : (
        <DataTable columns={columns} rows={logs || []} emptyTitle="No audit entries" />
      )}
    </div>
  );
}

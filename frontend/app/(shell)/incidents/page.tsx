"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import SeverityBadge from "@/components/SeverityBadge";
import ErrorState from "@/components/ErrorState";

export default function IncidentsPage() {
  const router = useRouter();
  const { data: incidents, error, reload } = useApiData<any[]>("/api/incidents", { pollMs: 8000 });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ title: "", incident_type: "manual", priority: "MEDIUM", location: "", description: "" });
  const [formError, setFormError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    try {
      const inc = await api.post<any>("/api/incidents", form);
      setShowForm(false);
      router.push(`/incidents/${inc.id}`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not create incident");
    }
  }

  const columns: Column<any>[] = [
    { key: "id", label: "Incident ID", render: (i) => i.id.replace("inc_", "INC-").toUpperCase() },
    { key: "title", label: "Title" },
    { key: "priority", label: "Priority", render: (i) => <SeverityBadge severity={i.priority} /> },
    { key: "status", label: "Status" },
    { key: "created_at", label: "Created", render: (i) => new Date(i.created_at).toLocaleString() },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Incident Management</h1>
        <button onClick={() => setShowForm((s) => !s)} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">
          {showForm ? "CANCEL" : "CREATE INCIDENT"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={create} className="bg-panel border border-border rounded-lg p-4 space-y-3 max-w-lg">
          <input required placeholder="Incident title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm" />
          <div className="grid grid-cols-2 gap-3">
            <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="bg-panel2 border border-border rounded px-3 py-2 text-sm">
              <option>LOW</option><option>MEDIUM</option><option>HIGH</option><option>CRITICAL</option>
            </select>
            <input placeholder="Location" value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} className="bg-panel2 border border-border rounded px-3 py-2 text-sm" />
          </div>
          <textarea placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="w-full bg-panel2 border border-border rounded px-3 py-2 text-sm" rows={3} />
          {formError && <div className="text-xs text-critical">{formError}</div>}
          <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">SAVE DRAFT / CREATE</button>
        </form>
      )}

      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable columns={columns} rows={incidents || []} onRowClick={(i) => router.push(`/incidents/${i.id}`)} emptyTitle="No incidents yet" />
      )}
    </div>
  );
}

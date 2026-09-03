"use client";
import { useState } from "react";
import { useParams } from "next/navigation";
import { api, API_BASE, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import SeverityBadge from "@/components/SeverityBadge";
import ErrorState from "@/components/ErrorState";

const TABS = ["Overview", "Timeline", "Evidence", "Notes"] as const;

export default function IncidentDetailPage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const { data: incident, error, reload: reloadIncident } = useApiData<any>(`/api/incidents/${incidentId}`);
  const { data: timelineData, error: timelineError, reload: reloadTimeline } = useApiData<any>(`/api/incidents/${incidentId}/timeline`);
  const { data: evidenceData, error: evidenceError, reload: reloadEvidence } = useApiData<any[]>(`/api/evidence?incident_id=${incidentId}`);
  const timeline = timelineData?.events || [];
  const evidence = evidenceData || [];

  const [tab, setTab] = useState<(typeof TABS)[number]>("Overview");
  const [note, setNote] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  function reloadAll() {
    reloadIncident();
    reloadTimeline();
    reloadEvidence();
  }

  async function changeStatus(status: string) {
    setActionError(null);
    try {
      if (status === "closed") await api.post(`/api/incidents/${incidentId}/close`);
      reloadAll();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not update incident status");
    }
  }

  async function addNote(e: React.FormEvent) {
    e.preventDefault();
    if (!note.trim()) return;
    setActionError(null);
    try {
      await api.post(`/api/incidents/${incidentId}/notes`, { text: note });
      setNote("");
      reloadAll();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not add note");
    }
  }

  function downloadPackage(fmt: "json" | "pdf") {
    window.open(`${API_BASE}/api/evidence/incidents/${incidentId}/package?fmt=${fmt}`, "_blank");
  }

  if (error) return <ErrorState message={`Incident ${incidentId} could not be loaded: ${error}`} onRetry={reloadIncident} />;
  if (!incident) return null;

  return (
    <div className="max-w-4xl space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-xs text-slate-500">INCIDENT #{incident.id.toUpperCase()}</div>
          <h1 className="text-lg font-semibold">{incident.title}</h1>
        </div>
        <div className="flex items-center gap-2">
          <SeverityBadge severity={incident.priority} />
          <span className="badge bg-slate-500/15 text-slate-300 border border-slate-500/30">{incident.status.toUpperCase()}</span>
        </div>
      </div>

      <div className="flex gap-2 border-b border-border">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`text-xs px-3 py-2 border-b-2 ${tab === t ? "border-accent text-accent" : "border-transparent text-slate-400"}`}>
            {t}
          </button>
        ))}
      </div>

      {actionError && <div className="text-xs text-critical">{actionError}</div>}

      {tab === "Overview" && (
        <div className="bg-panel border border-border rounded-lg p-4 text-sm space-y-2">
          <div><span className="text-slate-500">Location:</span> {incident.location || "—"}</div>
          <div><span className="text-slate-500">Description:</span> {incident.description || "—"}</div>
          <div><span className="text-slate-500">Camera:</span> {incident.camera_id || "—"}</div>
          <div><span className="text-slate-500">Created:</span> {new Date(incident.created_at).toLocaleString()}</div>
          <div className="flex gap-2 pt-3">
            <button onClick={() => downloadPackage("json")} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">GENERATE EVIDENCE PACKAGE (JSON)</button>
            <button onClick={() => downloadPackage("pdf")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">EXPORT PDF</button>
            {incident.status !== "closed" && (
              <button onClick={() => changeStatus("closed")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-critical text-critical ml-auto">CLOSE INCIDENT</button>
            )}
          </div>
        </div>
      )}

      {tab === "Timeline" && (
        timelineError ? (
          <ErrorState message={timelineError} onRetry={reloadTimeline} />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border">
            {timeline.map((e: any, i: number) => (
              <div key={i} className="px-3 py-2 flex justify-between text-sm">
                <span>{e.label}</span>
                <span className="text-xs text-slate-500">{new Date(e.timestamp).toLocaleString()}</span>
              </div>
            ))}
            {timeline.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">No timeline events yet.</div>}
          </div>
        )
      )}

      {tab === "Evidence" && (
        evidenceError ? (
          <ErrorState message={evidenceError} onRetry={reloadEvidence} />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border">
            {evidence.map((e) => (
              <div key={e.id} className="px-3 py-2 flex justify-between text-sm">
                <span>{e.evidence_type} · {e.verification_status}</span>
                {e.file_path && <a href={`${API_BASE}/api/evidence/${e.id}/file`} target="_blank" className="text-accent text-xs hover:underline">VIEW</a>}
              </div>
            ))}
            {evidence.length === 0 && <div className="px-3 py-4 text-xs text-slate-500">No evidence attached yet.</div>}
          </div>
        )
      )}

      {tab === "Notes" && (
        <div className="space-y-3">
          <form onSubmit={addNote} className="flex gap-2">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Add investigation note..." className="flex-1 bg-panel2 border border-border rounded px-3 py-2 text-sm" />
            <button className="text-xs bg-accent text-ink font-medium rounded px-3 py-2">ADD NOTE</button>
          </form>
        </div>
      )}
    </div>
  );
}

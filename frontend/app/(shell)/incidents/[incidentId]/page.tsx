"use client";
import { useState } from "react";
import { useParams } from "next/navigation";
import { api, openTokenedResource, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import SeverityBadge from "@/components/SeverityBadge";
import ErrorState from "@/components/ErrorState";
import EvidenceIntegrityBadge from "@/components/EvidenceIntegrityBadge";

const TABS = ["Overview", "Timeline", "Evidence", "Notes"] as const;

export default function IncidentDetailPage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const { data: incident, error, reload: reloadIncident } = useApiData<any>(`/api/incidents/${incidentId}`);
  const { data: timelineData, error: timelineError, reload: reloadTimeline } = useApiData<any>(`/api/incidents/${incidentId}/timeline`);
  const { data: summary, error: summaryError } = useApiData<any>(`/api/incidents/${incidentId}/summary`);
  const { data: evidenceData, error: evidenceError, reload: reloadEvidence } = useApiData<any[]>(`/api/evidence?incident_id=${incidentId}`);
  const { data: camerasData } = useApiData<any[]>("/api/cameras");
  const timeline = timelineData?.events || [];
  const evidence = evidenceData || [];
  const cameras = camerasData || [];

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

  async function downloadPackage(fmt: "json" | "pdf") {
    setActionError(null);
    try {
      await openTokenedResource(
        `/api/evidence/incidents/${incidentId}/package-token`,
        `/api/evidence/incidents/${incidentId}/package?fmt=${fmt}`
      );
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not generate evidence package");
    }
  }

  if (error) return <ErrorState message={`Incident ${incidentId} could not be loaded: ${error}`} onRetry={reloadIncident} />;
  if (!incident) {
    return (
      <div className="flex items-center gap-2 text-xs text-slate-500 py-8">
        <span className="inline-block h-3 w-3 rounded-full border-2 border-slate-500 border-t-transparent animate-spin" />
        Loading incident {incidentId}…
      </div>
    );
  }

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
        <div className="space-y-4">
          {summary && !summaryError && (
            <div className="bg-panel border border-border rounded-lg p-4 text-sm space-y-3">
              <div className="text-xs font-semibold text-slate-400 tracking-wide">INVESTIGATOR SUMMARY</div>

              {summary.why?.length > 0 && (
                <div>
                  <div className="text-slate-500 text-xs mb-1">Why was this flagged?</div>
                  <ul className="list-disc list-inside space-y-0.5">
                    {summary.why.map((reason: string, i: number) => <li key={i}>{reason}</li>)}
                  </ul>
                </div>
              )}

              <div>
                <div className="text-slate-500 text-xs mb-1">Risk score: {summary.risk.score}/100</div>
                {summary.risk.factors?.length > 0 && (
                  <ul className="space-y-0.5">
                    {summary.risk.factors.map((f: any, i: number) => (
                      <li key={i} className="text-xs"><span className="text-accent">+{f.points}</span> {f.label} — {f.detail}</li>
                    ))}
                  </ul>
                )}
              </div>

              {summary.vehicle && (
                <div>
                  <div className="text-slate-500 text-xs mb-1">Vehicle</div>
                  <div>
                    Plate <span className="font-mono">{summary.vehicle.plate_text || "—"}</span>
                    {" · "}confidence {Math.round((summary.vehicle.plate_confidence || 0) * 100)}%
                    {" · "}{summary.vehicle.total_plate_reads} observation(s)
                  </div>
                  {summary.vehicle.watchlist_match && (
                    <div className="text-xs text-critical">
                      Watchlist match ({summary.vehicle.watchlist_match.priority}): {summary.vehicle.watchlist_match.reason || "no reason recorded"}
                    </div>
                  )}
                </div>
              )}

              {summary.where && (
                <div>
                  <div className="text-slate-500 text-xs mb-1">Where — {summary.where.cameras_visited} camera(s)</div>
                  <div className="text-xs">
                    {summary.where.route?.map((hop: any, i: number) => (
                      <span key={i}>{hop.camera_code}{i < summary.where.route.length - 1 ? " → " : ""}</span>
                    ))}
                    {(!summary.where.route || summary.where.route.length === 0) && "—"}
                  </div>
                </div>
              )}

              <div>
                <div className="text-slate-500 text-xs mb-1">Evidence integrity</div>
                <div className="flex flex-wrap gap-2">
                  {summary.evidence?.map((e: any) => <EvidenceIntegrityBadge key={e.id} status={e.verification_status} />)}
                  {(!summary.evidence || summary.evidence.length === 0) && <span className="text-xs text-slate-500">No evidence attached yet.</span>}
                </div>
              </div>

              {summary.related_alerts?.length > 1 && (
                <div className="text-xs text-slate-500">{summary.related_alerts.length} correlated alerts on this incident.</div>
              )}
            </div>
          )}

          <div className="bg-panel border border-border rounded-lg p-4 text-sm space-y-2">
            <div><span className="text-slate-500">Location:</span> {incident.location || "—"}</div>
            <div><span className="text-slate-500">Description:</span> {incident.description || "—"}</div>
            <div><span className="text-slate-500">Camera:</span> {incident.camera_id ? (cameras.find((c) => c.id === incident.camera_id)?.camera_code || incident.camera_id) : "—"}</div>
            <div><span className="text-slate-500">Created:</span> {new Date(incident.created_at).toLocaleString()}</div>
            <div className="flex gap-2 pt-3">
              <button onClick={() => downloadPackage("json")} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">GENERATE EVIDENCE PACKAGE (JSON)</button>
              <button onClick={() => downloadPackage("pdf")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">EXPORT PDF</button>
              {incident.status !== "closed" && (
                <button onClick={() => changeStatus("closed")} className="text-xs border border-border rounded px-3 py-1.5 hover:border-critical text-critical ml-auto">CLOSE INCIDENT</button>
              )}
            </div>
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
              <div key={e.id} className="px-3 py-2 flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">{e.evidence_type} <EvidenceIntegrityBadge status={e.verification_status} /></span>
                {e.file_path && (
                  <button
                    onClick={() => openTokenedResource(`/api/evidence/${e.id}/file-token`, `/api/evidence/${e.id}/file`)}
                    className="text-accent text-xs hover:underline"
                  >
                    VIEW
                  </button>
                )}
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

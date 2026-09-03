"use client";
import { useState } from "react";
import { useParams } from "next/navigation";
import { api, API_BASE, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";

export default function EvidenceDetailPage() {
  const { evidenceId } = useParams<{ evidenceId: string }>();
  const { data: evidence, error, reload } = useApiData<any>(`/api/evidence/${evidenceId}`);
  const [actionError, setActionError] = useState<string | null>(null);

  async function verify() {
    setActionError(null);
    try {
      await api.post(`/api/evidence/${evidenceId}/verify`);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Verification request failed");
    }
  }

  if (error) return <ErrorState message={`Evidence ${evidenceId} could not be loaded: ${error}`} onRetry={reload} />;
  if (!evidence) return null;
  const isImage = evidence.file_path?.match(/\.(jpg|jpeg|png)$/i);

  return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-lg font-semibold">Evidence {evidence.id}</h1>
      {isImage && (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={`${API_BASE}/api/evidence/${evidenceId}/file`} alt="evidence" className="rounded-lg border border-border max-w-full" />
      )}
      <div className="bg-panel border border-border rounded-lg p-4 text-sm space-y-2">
        <div><span className="text-slate-500">Type:</span> {evidence.evidence_type}</div>
        <div><span className="text-slate-500">Source Camera:</span> {evidence.camera_id || "—"}</div>
        <div><span className="text-slate-500">Case:</span> {evidence.incident_id || "—"}</div>
        <div><span className="text-slate-500">Captured:</span> {new Date(evidence.created_at).toLocaleString()}</div>
        <div><span className="text-slate-500">Verification:</span> {evidence.verification_status}</div>
        {evidence.sha256 && <div><span className="text-slate-500">SHA-256:</span> <span className="font-mono text-xs break-all">{evidence.sha256}</span></div>}
      </div>
      {actionError && <div className="text-xs text-critical">{actionError}</div>}
      <div className="flex gap-2">
        {evidence.file_path && (
          <a href={`${API_BASE}/api/evidence/${evidenceId}/file`} target="_blank" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">DOWNLOAD</a>
        )}
        {evidence.verification_status !== "verified" && (
          <button onClick={verify} className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">VERIFY (COMPUTE HASH)</button>
        )}
      </div>
    </div>
  );
}

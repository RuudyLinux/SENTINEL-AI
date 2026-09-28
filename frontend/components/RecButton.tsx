"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, getStoredUser, ApiError } from "@/lib/api";

// same roles as the backend's recording endpoints
const CAN_RECORD = ["Administrator", "Control Room Operator"];

function mmss(seconds: number) {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/** REC: records the annotated live view to MP4 on the backend, saved as
 * hashed evidence when stopped (or at the length cap). `recording` comes from
 * the camera row so a recording started elsewhere shows up here too. */
export default function RecButton({ cameraId, online, recording: recordingProp, onChange }: {
  cameraId: string; online: boolean; recording: boolean; onChange?: () => void;
}) {
  const [canRecord, setCanRecord] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [maxSeconds, setMaxSeconds] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    const user = getStoredUser();
    setCanRecord(!!user && CAN_RECORD.includes(user.role));
  }, []);

  // the server stopped it (length cap, camera stopped): drop our copy
  const wasRecording = useRef(recordingProp);
  useEffect(() => {
    if (wasRecording.current && !recordingProp) setStartedAt(null);
    wasRecording.current = recordingProp;
  }, [recordingProp]);

  // pick up the start time for a recording we didn't start in this tab
  useEffect(() => {
    if (!recordingProp || startedAt !== null) return;
    api.get<any>(`/api/cameras/${cameraId}/recording`).then((s) => {
      if (s?.recording) {
        setStartedAt(Date.parse(s.started_at + "Z"));
        setMaxSeconds(s.max_seconds ?? null);
      }
    }).catch(() => {});
  }, [recordingProp, cameraId, startedAt]);

  useEffect(() => {
    if (startedAt === null) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [startedAt]);

  async function start() {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const s = await api.post<any>(`/api/cameras/${cameraId}/recording/start`);
      setStartedAt(Date.parse(s.started_at + "Z"));
      setMaxSeconds(s.max_seconds ?? null);
      setNow(Date.now());
      onChange?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start recording");
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    setError(null);
    try {
      const s = await api.post<any>(`/api/cameras/${cameraId}/recording/stop`);
      setStartedAt(null);
      if (s.evidence_id) setSaved(s.evidence_id);
      else setError(s.error || "Recording stopped but was not saved");
      onChange?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not stop recording");
    } finally {
      setBusy(false);
    }
  }

  // the camera row is polled, so trust our own start until it catches up
  const recording = recordingProp || startedAt !== null;
  const elapsed = startedAt ? (now - startedAt) / 1000 : 0;

  return (
    <div className="space-y-1">
      {recording ? (
        <button
          onClick={stop}
          disabled={busy || !canRecord}
          aria-pressed="true"
          className="w-full text-xs rounded py-2 font-medium border border-critical text-critical bg-critical/10 disabled:opacity-60 flex items-center justify-center gap-2"
          title={canRecord ? "Stop and save the recording as evidence" : "Only an Administrator or Control Room Operator can stop recordings"}
        >
          <span className="inline-block w-2 h-2 rounded-full bg-critical animate-pulse" aria-hidden="true" />
          {busy ? "SAVING…" : `STOP REC ${mmss(elapsed)}`}
          {maxSeconds ? <span className="text-slate-500 font-normal">/ {mmss(maxSeconds)}</span> : null}
        </button>
      ) : (
        <button
          onClick={start}
          disabled={busy || !online || !canRecord}
          className="w-full text-xs rounded py-2 font-medium border border-critical/60 text-critical hover:bg-critical/10 disabled:opacity-40 flex items-center justify-center gap-2"
          title={!canRecord ? "Only an Administrator or Control Room Operator can record"
            : !online ? "The camera has to be connected to record" : "Record this view (with AI boxes) as evidence"}
        >
          <span className="inline-block w-2 h-2 rounded-full bg-critical" aria-hidden="true" />
          {busy ? "STARTING…" : "REC"}
        </button>
      )}
      {error && <div className="text-xs text-critical">{error}</div>}
      {saved && (
        <div className="text-xs text-slate-400">
          Saved as evidence. <Link href={`/evidence/${saved}`} className="text-accent underline">Open recording</Link>
        </div>
      )}
    </div>
  );
}

/** Small "● REC mm:ss" marker for the video corner. */
export function RecIndicator({ recording }: { recording: boolean }) {
  if (!recording) return null;
  return (
    <span className="absolute top-2 left-2 text-[11px] font-semibold text-white bg-critical/90 rounded px-1.5 py-0.5 flex items-center gap-1">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-white animate-pulse" aria-hidden="true" />
      REC
    </span>
  );
}

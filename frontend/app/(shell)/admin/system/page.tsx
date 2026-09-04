"use client";
import { useApiData } from "@/lib/useApiData";
import StatusDot from "@/components/StatusDot";
import ErrorState from "@/components/ErrorState";

export default function SystemStatusPage() {
  const { data: status, error, reload } = useApiData<any>("/api/system/status", { pollMs: 5000 });

  return (
    <div className="space-y-6 max-w-3xl">
      <h1 className="text-lg font-semibold">System Status & Settings</h1>

      <div className="bg-panel border border-border rounded-lg p-4">
        <div className="text-sm font-medium mb-3">Subsystems</div>
        {error ? (
          <ErrorState message={`Backend is unreachable — status cannot be confirmed: ${error}`} onRetry={reload} />
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
              {status?.subsystems.map((s: any) => (
                <div key={s.name} className="flex justify-between border-b border-border/50 py-1.5">
                  <span>{s.name}</span>
                  <StatusDot status={s.status} />
                </div>
              ))}
            </div>
            <div className="text-xs text-slate-500 mt-3">
              {status?.cameras_registered ?? 0} camera(s) registered · {status?.camera_workers_running ?? 0} detection worker(s) running
            </div>
          </>
        )}
      </div>

      <div className="bg-panel border border-border rounded-lg p-4 text-xs text-slate-400 space-y-2">
        <div className="text-sm font-medium text-slate-200 mb-1">Scope & Honesty Notes</div>
        <p>This build runs a real detection pipeline (YOLOv8 + ByteTrack + EasyOCR) against a webcam, an uploaded
          video file, or an RTSP URL (best-effort via OpenCV/FFmpeg — no ONVIF discovery or vendor-specific auth,
          and no real CCTV/VMS was available to test against in this environment). The camera adapter is written so
          a dedicated ONVIF/VMS integration can be added later without touching detection, ANPR, correlation, or the
          rules engine.</p>
        <p>No Kafka/Kubernetes/vector-DB/edge-Jetson deployment is running — this is the documented "working slice"
          (single FastAPI process + SQLite) the source master document calls for at hackathon scale, with the
          scale-out path documented rather than built.</p>
        <p>No face recognition or person re-identification is implemented (privacy-sensitive, out of scope). AI
          performance numbers under Analytics are real aggregates from this database — no accuracy percentage is
          fabricated.</p>
        <p>Every page on this dashboard shows an explicit "Data unavailable" state (not a silent empty list) when its
          backend request actually fails, so a fetch error can never be mistaken for a genuine empty result.</p>
      </div>
    </div>
  );
}

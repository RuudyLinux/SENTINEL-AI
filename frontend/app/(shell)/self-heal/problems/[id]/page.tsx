"use client";
import { use } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";
import { SelfHealSeverityBadge, SelfHealStatusBadge } from "@/components/SelfHealBadges";

type EventDetail = {
  id: string; timestamp: string; component: string; camera_id: string | null; camera_code: string | null;
  error_type: string; severity: string; message: string; recovery_action: string;
  attempt: number; max_attempts: number; status: string; duration_seconds: number; endpoint: string;
  metadata: Record<string, unknown>;
};

export default function ProblemDetailsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { data, error, reload } = useApiData<EventDetail>(`/api/self-heal/events/${id}`, { pollMs: 8000 });

  return (
    <div className="space-y-4">
      <button onClick={() => router.back()} className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200">
        <ArrowLeft size={14} /> Back
      </button>

      {error ? (
        <ErrorState message={`Problem detail could not be loaded: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <div className="space-y-6">
          <div>
            <div className="text-xs text-slate-500">PROBLEM #{data.id}</div>
            <h1 className="text-lg font-semibold mt-0.5">{data.error_type.replace(/_/g, " ")}</h1>
            <p className="text-sm text-slate-400 mt-1">{data.message}</p>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Field label="Component" value={data.camera_code ? `${data.component} (${data.camera_code})` : data.component} />
            <Field label="Severity" value={<SelfHealSeverityBadge severity={data.severity} />} />
            <Field label="Detected" value={new Date(data.timestamp).toLocaleString()} />
            <Field label="Recovery" value={data.recovery_action || "—"} />
            <Field label="Attempts" value={`${data.attempt} / ${data.max_attempts}`} />
            <Field label="Duration" value={`${data.duration_seconds.toFixed(2)}s`} />
            <Field label="Result" value={<SelfHealStatusBadge status={data.status} />} />
            {data.endpoint && <Field label="Endpoint" value={data.endpoint} />}
          </div>

          {/* Timeline derived honestly from this one real recorded row —
              we log the retry loop's final outcome (attempt count + total
              duration), not a per-retry timestamp series, so this shows
              exactly what was actually measured, nothing fabricated. */}
          <div>
            <h2 className="text-sm font-medium text-slate-300 mb-2">Timeline</h2>
            <div className="border border-border rounded-lg divide-y divide-border">
              <TimelineRow
                time={new Date(new Date(data.timestamp).getTime() - data.duration_seconds * 1000).toLocaleTimeString()}
                text="Error detected"
              />
              {data.attempt > 1 && (
                <TimelineRow time="" text={`Retry loop ran — ${data.attempt} of ${data.max_attempts} attempt(s) over ${data.duration_seconds.toFixed(2)}s`} />
              )}
              <TimelineRow
                time={new Date(data.timestamp).toLocaleTimeString()}
                text={data.status === "RECOVERED" ? "Recovered" : data.status === "FAILED" ? "Recovery failed — escalated" : data.status.replace(/_/g, " ")}
                emphasize
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="border border-border rounded-lg bg-panel px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-sm text-slate-100 mt-1">{value}</div>
    </div>
  );
}

function TimelineRow({ time, text, emphasize }: { time: string; text: string; emphasize?: boolean }) {
  return (
    <div className="px-4 py-2.5 flex items-center gap-3 text-sm">
      <span className="text-xs text-slate-500 w-20 shrink-0">{time}</span>
      <span className={emphasize ? "text-slate-100 font-medium" : "text-slate-300"}>{text}</span>
    </div>
  );
}

"use client";
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Pause, Play, Trash2, Car, User, ScanLine, ShieldAlert, FolderOpen, Wrench } from "lucide-react";
import { useApiData } from "@/lib/useApiData";
import { useLiveFeed, type FeedItem, type FeedKind } from "@/lib/useLiveFeed";
import KpiCard from "@/components/KpiCard";
import SeverityBadge from "@/components/SeverityBadge";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

/** Live-stream status for the page header.
 *
 * Deliberately not `ConnectionBadge` — that reports a CAMERA's connection
 * lifecycle. This is the dashboard's own WebSocket, a different fact, and
 * conflating them would let a healthy-looking badge hide a dead feed.
 */
function StreamStatus({ connected }: { connected: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-[10px] border rounded px-2 py-1 ${
        connected ? "text-ok border-ok/40 bg-ok/10" : "text-critical border-critical/40 bg-critical/10"
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-ok animate-pulse-subtle" : "bg-critical"}`} />
      {connected ? "LIVE STREAM" : "STREAM DOWN"}
    </span>
  );
}

/** Operator-facing groupings, in the order they appear as filter chips. */
const FILTERS: { kind: FeedKind; label: string; icon: typeof Car }[] = [
  { kind: "sighting", label: "Plates", icon: ScanLine },
  { kind: "alert", label: "Alerts", icon: ShieldAlert },
  { kind: "incident", label: "Incidents", icon: FolderOpen },
  { kind: "detection", label: "Detections", icon: Car },
  { kind: "system", label: "System", icon: Wrench },
];

function timeOf(item: FeedItem) {
  const raw = item.data?.timestamp || item.data?.created_at;
  const date = raw ? new Date(raw) : new Date(item.at);
  return Number.isNaN(date.getTime()) ? new Date(item.at) : date;
}

/** One row of the live stream.
 *
 * Alerts and watchlist hits are visually separated from routine detections
 * rather than merely coloured differently: an operator scanning a fast-moving
 * feed needs to find the actionable rows without reading them.
 */
function FeedRow({ item, onOpen }: { item: FeedItem; onOpen: (item: FeedItem) => void }) {
  const data = item.data ?? {};
  const isAlert = item.kind === "alert" || item.kind === "incident";
  const isWatchlist = item.kind === "sighting" && data.watchlist_flag;
  const clickable = item.kind === "sighting" || item.kind === "alert" || item.kind === "incident";

  return (
    <div
      onClick={() => clickable && onOpen(item)}
      className={[
        "px-3 py-2 grid grid-cols-[9rem_7rem_1fr_5rem] gap-3 items-center text-sm border-l-2",
        isAlert || isWatchlist
          ? "border-l-critical bg-critical/5"
          : "border-l-transparent hover:bg-panel2",
        clickable ? "cursor-pointer" : "",
      ].join(" ")}
    >
      <div className="flex items-center gap-2 min-w-0">
        {item.kind === "sighting" && (
          <span className="badge bg-accent/15 text-accent border border-accent/30 font-mono truncate">
            {data.plate_text || "—"}
          </span>
        )}
        {item.kind === "alert" && <SeverityBadge severity={data.severity} />}
        {item.kind === "incident" && <SeverityBadge severity={data.priority} />}
        {item.kind === "detection" && (
          <span className="badge bg-slate-500/15 text-slate-300 border border-slate-500/30 inline-flex items-center gap-1">
            {data.cls === "person" ? <User size={11} /> : <Car size={11} />}
            {data.cls}
          </span>
        )}
        {item.kind === "system" && (
          <span className="badge bg-slate-500/15 text-slate-400 border border-slate-500/30">
            {data.component || "system"}
          </span>
        )}
      </div>

      <span className="text-slate-300 truncate">{data.camera_code || data.camera_id || "—"}</span>

      <span className="text-slate-400 text-xs truncate">
        {item.kind === "sighting" && (
          <>
            {isWatchlist && <span className="text-critical font-medium">WATCHLIST MATCH · </span>}
            {Math.round((data.plate_confidence ?? 0) * 100)}% plate confidence
            {data.reads_count > 1 ? ` · ${data.reads_count} reads` : ""}
            {data.track_id ? ` · track ${data.track_id}` : ""}
          </>
        )}
        {item.kind === "alert" && (
          <>
            {data.risk_score ? <span className="text-slate-300">Risk {data.risk_score}/100 · </span> : null}
            {(data.reasons ?? []).join("; ")}
          </>
        )}
        {item.kind === "incident" && data.title}
        {item.kind === "detection" && (
          <>
            {Math.round((data.confidence ?? 0) * 100)}% confidence
            {data.track_id ? ` · track ${data.track_id}` : ""}
          </>
        )}
        {item.kind === "system" && (data.message || data.error_type)}
      </span>

      <span className="text-xs text-slate-500 text-right tabular-nums">
        {timeOf(item).toLocaleTimeString()}
      </span>
    </div>
  );
}

export default function LiveDetectionControlRoom() {
  const router = useRouter();
  const [active, setActive] = useState<FeedKind[]>(FILTERS.map((f) => f.kind));
  const { items, counts, connected, paused, setPaused, clear } = useLiveFeed();

  // Camera state still comes from the API (it is state, not an event stream);
  // the feed below is pure push. Polled slowly — the live stream carries the
  // fast-moving part.
  const { data: cameras, error: camerasError, reload } = useApiData<any[]>("/api/cameras", { pollMs: 15000 });
  const onlineCameras = (cameras || []).filter((c) => c.status === "online").length;
  const aiCameras = (cameras || []).filter((c) => c.grid_state === "PROCESSING").length;

  const visible = useMemo(() => items.filter((i) => active.includes(i.kind)), [items, active]);

  function toggle(kind: FeedKind) {
    setActive((prev) => (prev.includes(kind) ? prev.filter((k) => k !== kind) : [...prev, kind]));
  }

  function open(item: FeedItem) {
    if (item.kind === "sighting" && item.data?.vehicle_id) {
      router.push(`/vehicles/${item.data.vehicle_id}`);
    } else if (item.kind === "alert" && item.data?.id) {
      router.push(`/alerts/${item.data.id}`);
    } else if (item.kind === "incident" && item.data?.id) {
      router.push(`/investigate?case=${item.data.id}`);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Live AI Detection</h1>
        <StreamStatus connected={connected} />
      </div>

      {camerasError ? (
        <ErrorState message={camerasError} onRetry={reload} />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <KpiCard title="Cameras Online" value={`${onlineCameras}/${cameras?.length ?? 0}`} sub="Streaming now" />
          <KpiCard title="Running AI" value={aiCameras} sub="Inference active" />
          <KpiCard title="Plates This Session" value={counts.sighting} sub="Recognized since page open" />
          <KpiCard title="Alerts This Session" value={counts.alert} sub="Since page open" />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map(({ kind, label, icon: Icon }) => (
          <button
            key={kind}
            onClick={() => toggle(kind)}
            className={`text-xs px-3 py-1.5 rounded border inline-flex items-center gap-1.5 transition-colors duration-fast ${
              active.includes(kind) ? "border-accent text-accent" : "border-border text-slate-500"
            }`}
          >
            <Icon size={12} strokeWidth={2.25} />
            {label}
            <span className="tabular-nums text-slate-500">{counts[kind]}</span>
          </button>
        ))}
        <div className="flex-1" />
        <button
          onClick={() => setPaused(!paused)}
          className="text-xs px-3 py-1.5 rounded border border-border text-slate-300 hover:border-accent inline-flex items-center gap-1.5"
        >
          {paused ? <Play size={12} /> : <Pause size={12} />}
          {paused ? "Resume" : "Pause"}
        </button>
        <button
          onClick={clear}
          className="text-xs px-3 py-1.5 rounded border border-border text-slate-300 hover:border-accent inline-flex items-center gap-1.5"
        >
          <Trash2 size={12} />
          Clear
        </button>
      </div>

      {paused && (
        <div className="text-xs text-yellow-400 border border-yellow-500/30 bg-yellow-500/10 rounded px-3 py-2">
          Feed paused — the system is still detecting and the counters above keep advancing. Resume to see new events.
        </div>
      )}

      {visible.length === 0 ? (
        <EmptyState
          title={connected ? "Waiting for live events" : "Not connected to the live stream"}
          hint={
            connected
              ? "Events appear here as cameras detect. Start a camera's AI under Cameras → Camera Control Center."
              : "The dashboard reconnects automatically. Check that the backend is running."
          }
        />
      ) : (
        <div className="border border-border rounded-lg divide-y divide-border max-h-[32rem] overflow-y-auto">
          {visible.map((item) => (
            <FeedRow key={item.key} item={item} onOpen={open} />
          ))}
        </div>
      )}
    </div>
  );
}

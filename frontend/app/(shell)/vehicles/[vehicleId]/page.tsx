"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useParams, useRouter } from "next/navigation";
import { ChevronLeft, ChevronRight, Pause, Play, ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import { useLiveSocket, type LiveEvent } from "@/lib/useLiveSocket";
import { EVENT } from "@/lib/useLiveFeed";
import SeverityBadge from "@/components/SeverityBadge";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

const CameraMap = dynamic(() => import("@/components/CameraMap"), { ssr: false });

/** Milliseconds each hop is held during journey replay. Slow enough to read the
 * camera and timestamp, fast enough that a long journey is not a chore. */
const REPLAY_INTERVAL_MS = 1400;

function RiskPanel({ score, severity, factors }: { score: number; severity: string; factors: any[] }) {
  return (
    <div className="rounded-lg border border-border bg-panel p-4 space-y-3">
      <div className="flex items-baseline justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Risk Score</span>
        <SeverityBadge severity={severity} />
      </div>
      <div className="text-3xl font-semibold text-slate-100">
        {score}
        <span className="text-base text-slate-500">/100</span>
      </div>
      {/* Every point is attributed. The score is a transparent weighted sum,
          not a model output, and the UI has to be able to prove that. */}
      {factors.length === 0 ? (
        <p className="text-xs text-slate-500">
          No risk factors recorded — nothing about this vehicle has raised a signal.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {factors.map((f) => (
            <li key={f.factor} className="flex gap-2 text-xs">
              <span className="text-accent font-mono tabular-nums w-8 shrink-0">+{f.points}</span>
              <span className="text-slate-300 shrink-0">{f.label}</span>
              <span className="text-slate-500 truncate">{f.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function VehicleInvestigationPage() {
  const router = useRouter();
  const params = useParams<{ vehicleId: string }>();
  const vehicleId = params?.vehicleId;

  const { data: summary, error: summaryError, reload: reloadSummary } =
    useApiData<any>(vehicleId ? `/api/vehicles/${vehicleId}/summary` : null);
  const { data: route, error: routeError, reload: reloadRoute } =
    useApiData<any>(vehicleId ? `/api/vehicles/${vehicleId}/route` : null);
  const { data: sightings } =
    useApiData<any[]>(vehicleId ? `/api/vehicles/${vehicleId}/sightings` : null);
  const { data: cameras } = useApiData<any[]>("/api/cameras");

  const [replayIndex, setReplayIndex] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const hops = useMemo(() => route?.sightings ?? [], [route]);

  // A new sighting for THIS vehicle arriving live extends the journey without a
  // page reload — the core "if another camera sees it, the screen updates"
  // behavior. Filtered by vehicle so an unrelated detection never refetches.
  useLiveSocket(
    useCallback(
      (e: LiveEvent) => {
        if (e.type === EVENT.VEHICLE_SIGHTING && e.data?.vehicle_id === vehicleId) {
          reloadRoute();
          reloadSummary();
        }
      },
      [vehicleId, reloadRoute, reloadSummary],
    ),
  );

  useEffect(() => {
    if (!playing || hops.length === 0) return;
    timerRef.current = setInterval(() => {
      setReplayIndex((prev) => {
        const next = (prev ?? -1) + 1;
        if (next >= hops.length) {
          setPlaying(false);
          return hops.length - 1;
        }
        return next;
      });
    }, REPLAY_INTERVAL_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [playing, hops.length]);

  if (summaryError) return <ErrorState message={summaryError} onRetry={reloadSummary} />;
  if (!summary) return <div className="text-sm text-slate-400">Loading vehicle…</div>;

  const activeHop = replayIndex !== null ? hops[replayIndex] : null;
  const mapCameras = (cameras || []).filter((c: any) => hops.some((h: any) => h.camera_id === c.id));
  const routePoints = hops.map((h: any) => ({
    lat: h.lat,
    lng: h.lng,
    label: `${h.camera_code} — ${new Date(h.timestamp).toLocaleTimeString()}`,
  }));

  function step(delta: number) {
    setPlaying(false);
    setReplayIndex((prev) => {
      const next = Math.min(hops.length - 1, Math.max(0, (prev ?? 0) + delta));
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <button
        onClick={() => router.push("/vehicles")}
        className="text-xs text-slate-400 hover:text-accent inline-flex items-center gap-1"
      >
        <ArrowLeft size={12} /> All vehicles
      </button>

      {/* --- Vehicle summary --- */}
      <div className="rounded-lg border border-border bg-panel p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold font-mono">{summary.vehicle.plate_text || "Unidentified vehicle"}</h1>
          {/* "LIVE" is asserted only from a genuinely recent sighting; anything
              older is explicitly labelled as a last known position. */}
          {summary.is_live ? (
            <span className="badge bg-ok/15 text-ok border border-ok/30 inline-flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-ok animate-pulse-subtle" /> LIVE
            </span>
          ) : (
            <span className="badge bg-slate-500/15 text-slate-400 border border-slate-500/30">LAST KNOWN</span>
          )}
          {summary.watchlist_flag && (
            <span className="badge bg-critical/15 text-critical border border-critical/30">WATCHLIST</span>
          )}
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 text-sm">
          <div>
            <div className="text-xs text-slate-500">Current / last camera</div>
            <div className="text-slate-200">{summary.current_camera_code || "—"}</div>
            <div className="text-xs text-slate-500">
              {summary.current_seen_at ? new Date(summary.current_seen_at).toLocaleString() : "—"}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Vehicle type</div>
            <div className="text-slate-200">{summary.vehicle.vehicle_type || "—"}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Sightings / cameras</div>
            <div className="text-slate-200">
              {summary.total_sightings} / {summary.cameras_visited}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Best plate read</div>
            <div className="text-slate-200">{Math.round((summary.best_plate_confidence ?? 0) * 100)}%</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">First seen</div>
            <div className="text-slate-200">
              {summary.first_seen ? new Date(summary.first_seen).toLocaleString() : "—"}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Last seen</div>
            <div className="text-slate-200">
              {summary.last_seen ? new Date(summary.last_seen).toLocaleString() : "—"}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Alerts</div>
            <div className="text-slate-200">{summary.alert_count}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Incidents</div>
            <div className="text-slate-200">{summary.incident_count}</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <RiskPanel
          score={summary.risk_score}
          severity={summary.risk_severity}
          factors={summary.risk_factors ?? []}
        />

        {/* --- Journey + replay --- */}
        <div className="lg:col-span-2 rounded-lg border border-border bg-panel p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-slate-200">Journey</h2>
            {hops.length > 0 && (
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => step(-1)}
                  className="text-xs border border-border rounded px-2 py-1 hover:border-accent"
                  aria-label="Previous sighting"
                >
                  <ChevronLeft size={12} />
                </button>
                <button
                  onClick={() => {
                    if (!playing && (replayIndex === null || replayIndex >= hops.length - 1)) setReplayIndex(-1);
                    setPlaying(!playing);
                  }}
                  className="text-xs border border-border rounded px-3 py-1 hover:border-accent inline-flex items-center gap-1.5"
                >
                  {playing ? <Pause size={12} /> : <Play size={12} />}
                  {playing ? "Pause" : "Replay Journey"}
                </button>
                <button
                  onClick={() => step(1)}
                  className="text-xs border border-border rounded px-2 py-1 hover:border-accent"
                  aria-label="Next sighting"
                >
                  <ChevronRight size={12} />
                </button>
              </div>
            )}
          </div>

          {routeError ? (
            <ErrorState message={routeError} onRetry={reloadRoute} />
          ) : hops.length === 0 ? (
            <EmptyState
              title="No sightings recorded"
              hint="This vehicle has been recognized but not yet observed by a camera with a stored sighting."
            />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                {hops.map((h: any, i: number) => (
                  <button
                    key={h.plate_id ?? i}
                    onClick={() => {
                      setPlaying(false);
                      setReplayIndex(i);
                    }}
                    className={`px-2 py-1 rounded border font-mono transition-colors duration-fast ${
                      replayIndex === i
                        ? "border-brand-orange text-brand-orange bg-brand-orange/10"
                        : "border-border text-slate-300 hover:border-accent"
                    }`}
                  >
                    {h.camera_code}
                    <span className="text-slate-500 ml-1.5">
                      {new Date(h.timestamp).toLocaleTimeString()}
                    </span>
                  </button>
                ))}
              </div>

              {activeHop && (
                <div className="text-xs text-slate-400 border border-border rounded px-3 py-2 bg-panel2">
                  <span className="text-slate-200 font-medium">{activeHop.camera_code}</span>
                  {activeHop.location ? ` · ${activeHop.location}` : ""} ·{" "}
                  {new Date(activeHop.timestamp).toLocaleString()}
                  {activeHop.dwell_seconds > 0 && ` · dwelled ${Math.round(activeHop.dwell_seconds)}s`}
                  {` · ${Math.round((activeHop.confidence ?? 0) * 100)}% read`}
                  {activeHop.reads_count > 1 && ` over ${activeHop.reads_count} reads`}
                </div>
              )}

              <div className="h-72 border border-border rounded-lg overflow-hidden">
                <CameraMap
                  cameras={mapCameras}
                  route={routePoints}
                  activeIndex={replayIndex ?? undefined}
                  center={activeHop ? [activeHop.lat, activeHop.lng] : undefined}
                />
              </div>
              {/* The system knows where cameras saw this vehicle and when, and
                  nothing in between. Saying so on the map itself prevents the
                  route line being read as a GPS track. */}
              <p className="text-[11px] text-slate-500">
                Reconstructed camera-to-camera route from {hops.length} observed sighting
                {hops.length === 1 ? "" : "s"}. Lines connect consecutive sightings — they are not a
                recorded GPS path, and the vehicle's route between cameras is not known.
              </p>
            </>
          )}
        </div>
      </div>

      {/* --- Detection evidence --- */}
      <div className="space-y-2">
        <h2 className="text-sm font-medium text-slate-200">Detection Evidence</h2>
        {!sightings || sightings.length === 0 ? (
          <EmptyState title="No sighting records" hint="Evidence snapshots are captured when a plate is first recognized at a camera." />
        ) : (
          <div className="border border-border rounded-lg divide-y divide-border max-h-80 overflow-y-auto">
            {sightings.map((s: any) => (
              <div key={s.id} className="px-3 py-2 grid grid-cols-[8rem_1fr_6rem_7rem] gap-3 items-center text-sm">
                <span className="font-mono text-slate-300">{s.plate_text_normalized}</span>
                <span className="text-xs text-slate-500">
                  track {s.track_id ?? "—"} · {s.vehicle_class || "vehicle"} ·{" "}
                  {s.reads_count} read{s.reads_count === 1 ? "" : "s"}
                  {/* A null plate_bbox means OCR fell back to the whole vehicle
                      crop — a genuinely lower-quality read, shown rather than hidden. */}
                  {!s.plate_bbox && " · not localized"}
                  {/* Which preprocessing variant produced the read, and how many
                      variants agreed. Only meaningful once multi-variant reading
                      is enabled; hidden entirely on rows that predate it. */}
                  {s.ocr_variant && ` · ${s.ocr_variant}`}
                  {s.variants_agreeing > 1 && ` · ${s.variants_agreeing} variants agree`}
                </span>
                <span className="text-xs text-slate-400">
                  {Math.round((s.confidence ?? 0) * 100)}%
                  {/* Confidence and corroboration are separate facts. An
                      uncorroborated read is a real observation that only one
                      frame supports — shown as such rather than folded into the
                      percentage, which would misrepresent what OCR reported. */}
                  {s.corroborated === false && (
                    <span className="block text-[10px] text-amber-500/80">uncorroborated</span>
                  )}
                </span>
                <span className="text-xs text-slate-500 text-right">
                  {new Date(s.timestamp).toLocaleTimeString()}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

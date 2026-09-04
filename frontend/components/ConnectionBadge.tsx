// 24/7 auto-connect supervisor UI. `grid_state` (in-memory, set by
// worker.py._set_grid_state, exposed via GET /api/cameras) is the real
// connection-lifecycle truth for a camera whose worker has run in this
// backend process. REGISTERED/DISCONNECTED are synthesized client-side for
// the two cases grid_state is null — never started this process, vs
// previously connected and now not running — from the same DB-column facts
// (status, last_frame_at) the camera table already relied on before this.
export type ConnState =
  | "REGISTERED" | "CONNECTING" | "CONNECTED" | "PROCESSING"
  | "DEGRADED" | "RECONNECTING" | "DISCONNECTED" | "AUTH_ERROR" | "ERROR";

const STYLES: Record<ConnState, string> = {
  REGISTERED: "text-slate-400 border-slate-600 bg-slate-500/10",
  CONNECTING: "text-accent border-accent/40 bg-accent/10",
  CONNECTED: "text-ok border-ok/40 bg-ok/10",
  PROCESSING: "text-ok border-ok/40 bg-ok/15 font-semibold",
  DEGRADED: "text-high border-high/40 bg-high/10",
  RECONNECTING: "text-high border-high/40 bg-high/10",
  DISCONNECTED: "text-slate-500 border-border bg-transparent",
  AUTH_ERROR: "text-critical border-critical/40 bg-critical/10",
  ERROR: "text-critical border-critical/40 bg-critical/10",
};

export function deriveConnectionState(camera: any): ConnState {
  if (camera.grid_state) return camera.grid_state as ConnState;
  return camera.last_frame_at ? "DISCONNECTED" : "REGISTERED";
}

export default function ConnectionBadge({ camera }: { camera: any }) {
  const state = deriveConnectionState(camera);
  const style = STYLES[state] || STYLES.DISCONNECTED;
  const tooltipParts = [
    typeof camera.reconnect_count === "number" && camera.reconnect_count > 0
      ? `${camera.reconnect_count} reconnect${camera.reconnect_count === 1 ? "" : "s"}`
      : null,
    camera.last_error ? `Last error: ${camera.last_error}` : null,
  ].filter(Boolean);
  return (
    <span
      className={`inline-block text-[10px] border rounded px-1.5 py-0.5 whitespace-nowrap ${style}`}
      title={tooltipParts.length ? tooltipParts.join(" · ") : undefined}
    >
      {state.replace("_", " ")}
    </span>
  );
}

// AI processing is independent of connection state — a camera can be
// CONNECTED with AI off. Reflects the real per-camera flags (ai_person /
// ai_vehicle), not grid_state, so it stays accurate even before/after a
// PATCH that hasn't yet flipped grid_state to PROCESSING/CONNECTED.
export function AiBadge({ camera }: { camera: any }) {
  const on = !!(camera.ai_person || camera.ai_vehicle);
  return (
    <span
      className={`inline-block text-[10px] border rounded px-1.5 py-0.5 whitespace-nowrap ${
        on ? "text-accent border-accent/40 bg-accent/10" : "text-slate-500 border-border bg-transparent"
      }`}
    >
      AI {on ? "ON" : "OFF"}
    </span>
  );
}

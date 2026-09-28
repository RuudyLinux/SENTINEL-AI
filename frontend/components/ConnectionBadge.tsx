// grid_state (in memory, worker._set_grid_state, via GET /api/cameras) is the
// real connection state for a camera whose worker ran in this process.
// REGISTERED/DISCONNECTED are made up here for the two null cases (never
// started vs was connected and isn't running) from status and last_frame_at.
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

// AI is independent of connection; CONNECTED with AI off is normal. "AI ON"
// used to follow the flags alone, so a disconnected camera said
// "DISCONNECTED · AI ON" with nothing running. RUNNING = actually processing
// (grid_state PROCESSING), ENABLED = switched on but nothing processing now.
export function AiBadge({ camera }: { camera: any }) {
  const enabled = !!(camera.ai_person || camera.ai_vehicle);
  const running = enabled && camera.grid_state === "PROCESSING";
  const [text, style, title] = running
    ? ["AI RUNNING", "text-accent border-accent/40 bg-accent/10", "AI is processing this camera's live frames"]
    : enabled && camera.ai_blocked
      ? ["AI WAITING", "text-high border-high/40 bg-transparent", "All AI slots are busy: this camera streams without AI until its turn comes round"]
    : enabled
      ? ["AI ENABLED", "text-slate-400 border-border border-dashed bg-transparent", "AI is switched on for this camera and will run once it is connected"]
      : ["AI OFF", "text-slate-500 border-border bg-transparent", "AI is switched off for this camera"];
  return (
    <span className={`inline-block text-[10px] border rounded px-1.5 py-0.5 whitespace-nowrap ${style}`} title={title}>
      {text}
    </span>
  );
}

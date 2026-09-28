// shared AI-state label, was copy-pasted in cameras/control and
// self-heal/camera-health
export type CameraLike = { grid_state: string | null };

export function aiState(c: CameraLike): string {
  if (c.grid_state === "PROCESSING") return "AI RUNNING";
  if (c.grid_state === "CONNECTED") return "AI STOPPED";
  if (c.grid_state === "ERROR") return "AI ERROR";
  if (c.grid_state === "RECONNECTING" || c.grid_state === "CONNECTING") return "AI STARTING";
  return "—";
}

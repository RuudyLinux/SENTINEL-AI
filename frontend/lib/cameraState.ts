// Shared camera-AI-state label — final-review audit finding: this was
// duplicated verbatim in cameras/control/page.tsx and
// self-heal/camera-health/page.tsx, risking the two pages silently
// diverging if grid_state semantics ever change. One source of truth.
export type CameraLike = { grid_state: string | null };

export function aiState(c: CameraLike): string {
  if (c.grid_state === "PROCESSING") return "AI RUNNING";
  if (c.grid_state === "CONNECTED") return "AI STOPPED";
  if (c.grid_state === "ERROR") return "AI ERROR";
  if (c.grid_state === "RECONNECTING" || c.grid_state === "CONNECTING") return "AI STARTING";
  return "—";
}

/** Whether a camera (or route hop) has a real position. The backend stores
 * an unknown position as 0,0, which must never be plotted. */
export function hasLocation(p: { lat?: number | null; lng?: number | null } | null | undefined): boolean {
  if (!p) return false;
  const { lat, lng } = p;
  if (typeof lat !== "number" || typeof lng !== "number") return false;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return false;
  if (lat === 0 && lng === 0) return false;
  return Math.abs(lat) <= 90 && Math.abs(lng) <= 180;
}

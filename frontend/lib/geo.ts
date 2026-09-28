/** Does a camera (or route hop) have a real position? The backend stores
 * unknown as 0,0, and the grid catalogue has no coordinates, so every grid
 * camera arrives that way. 0,0 is in the Gulf of Guinea; plotting it put the
 * cameras there and routes across the Atlantic. */
export function hasLocation(p: { lat?: number | null; lng?: number | null } | null | undefined): boolean {
  if (!p) return false;
  const { lat, lng } = p;
  if (typeof lat !== "number" || typeof lng !== "number") return false;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return false;
  if (lat === 0 && lng === 0) return false;
  return Math.abs(lat) <= 90 && Math.abs(lng) <= 180;
}

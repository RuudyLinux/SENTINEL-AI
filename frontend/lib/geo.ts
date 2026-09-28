/** Whether a camera (or route hop) has a real map position.
 *
 * The backend stores an unknown position as 0,0: the Sentinel Grid catalogue
 * gives no coordinates, so all its cameras arrive that way. 0,0 is a point in
 * the Gulf of Guinea, and plotting it drew every grid camera there and routes
 * across the Atlantic. It is treated as "unknown", never as a place. */
export function hasLocation(p: { lat?: number | null; lng?: number | null } | null | undefined): boolean {
  if (!p) return false;
  const { lat, lng } = p;
  if (typeof lat !== "number" || typeof lng !== "number") return false;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return false;
  if (lat === 0 && lng === 0) return false;
  return Math.abs(lat) <= 90 && Math.abs(lng) <= 180;
}

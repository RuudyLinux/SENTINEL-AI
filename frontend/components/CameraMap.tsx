"use client";
import { useEffect, useMemo, useRef } from "react";
import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { hasLocation } from "@/lib/geo";

// Default marker icons reference assets Next.js won't resolve, so use divIcon.
// The dot is 14px inside a transparent 28px tap target (WCAG 2.5.8 minimum
// is 24px) so dense areas stay readable.
const ICON_BOX = 28; // >= the 24px WCAG minimum, with a little margin
const DOT = 14;

// count > 1: several cameras share one position; they get one marker showing
// the count, and the popup lists each camera.
const cameraIcon = (color: string, count = 1) =>
  L.divIcon({
    className: "",
    html: `<div style="width:${ICON_BOX}px;height:${ICON_BOX}px;display:flex;align-items:center;justify-content:center"><div style="width:${count > 1 ? DOT + 6 : DOT}px;height:${count > 1 ? DOT + 6 : DOT}px;border-radius:50%;background:${color};border:2px solid white;box-shadow:0 0 4px rgba(0,0,0,.6);color:#0b0f14;font:700 10px/1 sans-serif;display:flex;align-items:center;justify-content:center">${count > 1 ? count : ""}</div></div>`,
    // Leaflet centres a divIcon on its iconSize when no iconAnchor is set.
    iconSize: [ICON_BOX, ICON_BOX],
  });

// Same colours as StatusDot.
const STATUS_COLOR: Record<string, string> = { online: "#22c55e", degraded: "#f97316", offline: "#64748b" };
const RANK = ["online", "degraded", "offline"];

// a shared spot shows its best-off camera; the popup lists each one
function groupColor(group: any[]): string {
  const best = RANK.find((s) => group.some((c) => c.status === s)) || "offline";
  return STATUS_COLOR[best];
}

export default function CameraMap({
  cameras, route, center, activeIndex,
}: {
  cameras: any[];
  route?: { lat: number; lng: number; label: string }[];
  center?: [number, number];
  /** Route hop being replayed: later hops are dimmed and this one enlarged.
   * Undefined shows the whole route evenly. */
  activeIndex?: number;
}) {
  // Cameras without a position aren't drawn (never at 0,0); the overlay
  // reports how many.
  const located = cameras.filter(hasLocation);
  const unlocated = cameras.length - located.length;
  // Each hop keeps its index in the full route, which is what activeIndex counts.
  const routeOnMap = (route || []).map((r, i) => ({ ...r, i })).filter(hasLocation);
  const routeOffMap = (route?.length || 0) - routeOnMap.length;
  const first = located[0] || routeOnMap[0];
  const mapCenter: [number, number] = center || (first ? [first.lat, first.lng] : [23.03, 72.58]);

  // Plain Leaflet (react-leaflet's licence isn't OSI-approved). The map is
  // created once; overlays redraw only when their content changes, so a status
  // poll doesn't close an open popup.
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const overlays = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const map = L.map(container.current, { center: mapCenter, zoom: 12 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      className: "map-tiles-dark",
    }).addTo(map);
    mapRef.current = map;
    overlays.current = L.layerGroup().addTo(map);
    return () => {
      map.remove();
      mapRef.current = null;
      overlays.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refit only when positions change (e.g. a different route), not on every
  // status poll, so the view doesn't jump while the operator pans.
  const fitKey = useMemo(() => JSON.stringify([
    located.map((c) => [c.lat, c.lng]),
    routeOnMap.map((r) => [r.lat, r.lng]),
  ]), [located, routeOnMap]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || center) return;
    const points: [number, number][] = [
      ...located.map((c) => [c.lat, c.lng] as [number, number]),
      ...routeOnMap.map((r) => [r.lat, r.lng] as [number, number]),
    ];
    if (points.length === 0) return;
    // the container may have been sized after the map was made (tabs)
    map.invalidateSize();
    // Cameras can span the whole state, so fit to all of them.
    if (points.length === 1) map.setView(points[0], 15);
    else map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: 16 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  const signature = useMemo(() => JSON.stringify([
    located.map((c) => [c.id, c.lat, c.lng, c.camera_code, c.name, c.location, c.status, c.resolution]),
    routeOnMap.map((r) => [r.i, r.lat, r.lng, r.label]),
    activeIndex,
  ]), [located, routeOnMap, activeIndex]);

  useEffect(() => {
    const layer = overlays.current;
    if (!layer) return;
    layer.clearLayers();
    const spots = new Map<string, any[]>();
    for (const c of located) {
      const key = `${c.lat.toFixed(5)},${c.lng.toFixed(5)}`;
      spots.set(key, [...(spots.get(key) || []), c]);
    }
    for (const group of Array.from(spots.values())) {
      const online = group.filter((c) => c.status === "online").length;
      const lines = group.length === 1
        ? [
          { text: `${group[0].camera_code} — ${group[0].name}`, bold: true },
          { text: group[0].location || "" },
          { text: `${group[0].status} · ${group[0].resolution || "—"}` },
        ]
        : [
          { text: `${group.length} cameras here (${online} online)`, bold: true },
          ...group.map((c) => ({ text: `${c.camera_code} — ${c.name} · ${c.status}` })),
        ];
      L.marker([group[0].lat, group[0].lng], {
        title: group.map((c) => `${c.camera_code} — ${c.name} (${c.status})`).join("\n"),
        alt: group.length === 1 ? `Camera ${group[0].camera_code}` : `${group.length} cameras`,
        icon: cameraIcon(groupColor(group), group.length),
      })
        .bindPopup(popupContent(lines))
        .addTo(layer);
    }
    if (routeOnMap.length > 0) {
      // whole journey drawn faintly so the context stays during replay
      L.polyline(routeOnMap.map((r) => [r.lat, r.lng] as [number, number]), {
        color: "#2dd4bf", weight: 3, opacity: activeIndex === undefined ? 1 : 0.25,
      }).addTo(layer);
      // The portion travelled so far, drawn solid on top.
      const travelled = activeIndex === undefined ? [] : routeOnMap.filter((r) => r.i <= activeIndex);
      if (travelled.length > 1) {
        L.polyline(travelled.map((r) => [r.lat, r.lng] as [number, number]), { color: "#2dd4bf", weight: 4 }).addTo(layer);
      }
      for (const r of routeOnMap) {
        const isActive = activeIndex === r.i;
        const isPast = activeIndex !== undefined && r.i < activeIndex;
        const dimmed = activeIndex !== undefined && r.i > activeIndex;
        L.circleMarker([r.lat, r.lng], {
          radius: isActive ? 10 : 6,
          color: isActive ? "#f97316" : "#2dd4bf",
          fillOpacity: dimmed ? 0.25 : 1,
          opacity: dimmed ? 0.35 : 1,
          weight: isActive ? 3 : isPast ? 2 : 1,
        })
          .bindPopup(popupContent([{ text: r.label }]))
          .addTo(layer);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  return (
    <div className="relative" style={{ height: "100%", width: "100%" }}>
      {(unlocated > 0 || routeOffMap > 0) && (
        <div role="status" className="absolute top-2 right-2 z-[1000] max-w-[70%] rounded bg-panel2/90 border border-border px-2 py-1 text-xs text-slate-300">
          {unlocated > 0 && <div>{unlocated} camera{unlocated === 1 ? "" : "s"} not shown: location unavailable</div>}
          {routeOffMap > 0 && <div>{routeOffMap} sighting{routeOffMap === 1 ? "" : "s"} not drawn: camera location unavailable</div>}
        </div>
      )}
      {located.length > 0 && (
        <div className="absolute bottom-6 left-2 z-[1000] flex gap-3 rounded bg-panel2/90 border border-border px-2 py-1 text-xs text-slate-300">
          {RANK.map((s) => (
            <span key={s} className="flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-full border border-white" style={{ background: STATUS_COLOR[s] }} />
              {s}
            </span>
          ))}
        </div>
      )}
      <div ref={container} style={{ height: "100%", width: "100%", background: "#0b0f14" }} />
    </div>
  );
}

/** Popup body from text nodes: names and plates come from the DB and must
 * never be parsed as HTML. */
function popupContent(lines: { text: string; bold?: boolean }[]): HTMLElement {
  const root = document.createElement("div");
  root.className = "text-xs";
  for (const line of lines) {
    const row = document.createElement("div");
    if (line.bold) row.className = "font-semibold";
    row.textContent = line.text;
    root.appendChild(row);
  }
  return root;
}

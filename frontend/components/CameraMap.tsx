"use client";
import { useEffect, useMemo, useRef } from "react";
import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { hasLocation } from "@/lib/geo";

// default marker icons reference bundled assets Next.js won't resolve; use divIcon instead
//
// The DOT stays 14px and the TARGET is 28px. A measured pass at 375px width
// found 33 camera markers failing WCAG 2.5.8 (Target Size, Minimum, 24x24):
// the icon was 14x14 and markers in the same district overlap, so the spacing
// exception did not rescue them either — on a phone, picking one camera out
// of a cluster was a matter of luck. Growing the painted dot instead would
// have made a dense district unreadable, which is the wrong trade: the
// requirement is about the region that responds to a tap, not the glyph. So
// the dot is centred inside a transparent 28x28 box, and the map looks
// exactly as it did before.
const ICON_BOX = 28; // >= the 24px WCAG minimum, with a little margin
const DOT = 14;

const cameraIcon = (color: string) =>
  L.divIcon({
    className: "",
    html: `<div style="width:${ICON_BOX}px;height:${ICON_BOX}px;display:flex;align-items:center;justify-content:center"><div style="width:${DOT}px;height:${DOT}px;border-radius:50%;background:${color};border:2px solid white;box-shadow:0 0 4px rgba(0,0,0,.6)"></div></div>`,
    // Leaflet centres a divIcon on its iconSize when no iconAnchor is given,
    // so the dot still sits exactly on the camera's coordinate.
    iconSize: [ICON_BOX, ICON_BOX],
  });

export default function CameraMap({
  cameras, route, center, activeIndex,
}: {
  cameras: any[];
  route?: { lat: number; lng: number; label: string }[];
  center?: [number, number];
  /** Index of the route hop currently being replayed. When set, hops after it
   * are dimmed and the current one is enlarged, so the journey reads as a
   * progression rather than a finished line. Undefined shows the whole route
   * at equal weight — the existing behavior, unchanged for existing callers. */
  activeIndex?: number;
}) {
  // A camera with no known position is left off the map rather than drawn at
  // 0,0; the count below says how many, so none silently disappears.
  const located = cameras.filter(hasLocation);
  const unlocated = cameras.length - located.length;
  // Each hop keeps its index in the full route, which is what activeIndex counts.
  const routeOnMap = (route || []).map((r, i) => ({ ...r, i })).filter(hasLocation);
  const routeOffMap = (route?.length || 0) - routeOnMap.length;
  const first = located[0] || routeOnMap[0];
  const mapCenter: [number, number] = center || (first ? [first.lat, first.lng] : [23.03, 72.58]);

  // Plain Leaflet (BSD-2-Clause) rather than react-leaflet, whose Hippocratic
  // licence is not an OSI open-source licence. The map is created once, like
  // react-leaflet's MapContainer (its centre is fixed at mount), and the
  // overlays are redrawn only when what they show changes — every camera list
  // poll would otherwise close an open popup.
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

  const signature = useMemo(() => JSON.stringify([
    located.map((c) => [c.id, c.lat, c.lng, c.camera_code, c.name, c.location, c.status, c.resolution]),
    routeOnMap.map((r) => [r.i, r.lat, r.lng, r.label]),
    activeIndex,
  ]), [located, routeOnMap, activeIndex]);

  useEffect(() => {
    const layer = overlays.current;
    if (!layer) return;
    layer.clearLayers();
    for (const c of located) {
      L.marker([c.lat, c.lng], {
        title: `${c.camera_code} — ${c.name} (${c.status})`,
        alt: `Camera ${c.camera_code}`,
        icon: cameraIcon(c.status === "online" ? "#22c55e" : "#64748b"),
      })
        .bindPopup(popupContent([
          { text: `${c.camera_code} — ${c.name}`, bold: true },
          { text: c.location || "" },
          { text: `${c.status} · ${c.resolution || "—"}` },
        ]))
        .addTo(layer);
    }
    if (routeOnMap.length > 0) {
      // The full journey, drawn faintly — the route as a whole stays visible
      // during replay so the operator keeps the context.
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
      <div ref={container} style={{ height: "100%", width: "100%", background: "#0b0f14" }} />
    </div>
  );
}

/** Popup body built from text nodes: camera names and plate labels come from
 * the database, and must never be parsed as HTML. */
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

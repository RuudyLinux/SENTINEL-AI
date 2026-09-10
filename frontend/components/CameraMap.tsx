"use client";
import { MapContainer, TileLayer, Marker, Popup, Polyline, CircleMarker } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import L from "leaflet";

// default marker icons reference bundled assets Next.js won't resolve; use divIcon instead
const cameraIcon = (color: string) =>
  L.divIcon({
    className: "",
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid white;box-shadow:0 0 4px rgba(0,0,0,.6)"></div>`,
    iconSize: [14, 14],
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
  const mapCenter: [number, number] = center || (cameras[0] ? [cameras[0].lat, cameras[0].lng] : [23.03, 72.58]);
  return (
    <MapContainer center={mapCenter} zoom={12} style={{ height: "100%", width: "100%", background: "#0b0f14" }}>
      <TileLayer
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; OpenStreetMap contributors'
        className="map-tiles-dark"
      />
      {cameras.map((c) => (
        <Marker key={c.id} position={[c.lat, c.lng]} icon={cameraIcon(c.status === "online" ? "#22c55e" : "#64748b")}>
          <Popup>
            <div className="text-xs">
              <div className="font-semibold">{c.camera_code} — {c.name}</div>
              <div>{c.location}</div>
              <div>{c.status} · {c.resolution || "—"}</div>
            </div>
          </Popup>
        </Marker>
      ))}
      {route && route.length > 0 && (
        <>
          {/* The full journey, drawn faintly — the route as a whole stays
              visible during replay so the operator keeps the context. */}
          <Polyline
            positions={route.map((r) => [r.lat, r.lng])}
            pathOptions={{ color: "#2dd4bf", weight: 3, opacity: activeIndex === undefined ? 1 : 0.25 }}
          />
          {/* The portion travelled so far, drawn solid on top. */}
          {activeIndex !== undefined && activeIndex > 0 && (
            <Polyline
              positions={route.slice(0, activeIndex + 1).map((r) => [r.lat, r.lng])}
              pathOptions={{ color: "#2dd4bf", weight: 4 }}
            />
          )}
          {route.map((r, i) => {
            const isActive = activeIndex === i;
            const isPast = activeIndex !== undefined && i < activeIndex;
            const dimmed = activeIndex !== undefined && i > activeIndex;
            return (
              <CircleMarker
                key={i}
                center={[r.lat, r.lng]}
                radius={isActive ? 10 : 6}
                pathOptions={{
                  color: isActive ? "#f97316" : "#2dd4bf",
                  fillOpacity: dimmed ? 0.25 : 1,
                  opacity: dimmed ? 0.35 : 1,
                  weight: isActive ? 3 : isPast ? 2 : 1,
                }}
              >
                <Popup>{r.label}</Popup>
              </CircleMarker>
            );
          })}
        </>
      )}
    </MapContainer>
  );
}

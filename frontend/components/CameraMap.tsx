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
  cameras, route, center,
}: { cameras: any[]; route?: { lat: number; lng: number; label: string }[]; center?: [number, number] }) {
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
          <Polyline positions={route.map((r) => [r.lat, r.lng])} pathOptions={{ color: "#2dd4bf", weight: 3 }} />
          {route.map((r, i) => (
            <CircleMarker key={i} center={[r.lat, r.lng]} radius={6} pathOptions={{ color: "#2dd4bf", fillOpacity: 1 }}>
              <Popup>{r.label}</Popup>
            </CircleMarker>
          ))}
        </>
      )}
    </MapContainer>
  );
}

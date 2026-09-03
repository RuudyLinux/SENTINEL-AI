"use client";
import { useMemo, useState } from "react";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import KpiCard from "@/components/KpiCard";
import ErrorState from "@/components/ErrorState";

const CLASSES = ["all", "person", "car", "truck", "bus", "motorbike"];

export default function AiVisionPage() {
  const [cls, setCls] = useState("all");
  const { data: cams } = useApiData<any[]>("/api/cameras");
  const cameras = useMemo(() => Object.fromEntries((cams || []).map((c) => [c.id, c])), [cams]);

  const detectionsPath = cls === "all" ? "/api/detections" : `/api/detections?cls=${cls}`;
  const { data: detections, error, reload } = useApiData<any[]>(detectionsPath, { pollMs: 5000 });
  const detectionRows = detections || [];

  const personCount = detectionRows.filter((d) => d.cls === "person").length;
  const vehicleCount = detectionRows.filter((d) => d.cls !== "person").length;

  const columns: Column<any>[] = [
    { key: "timestamp", label: "Time", render: (d) => new Date(d.timestamp).toLocaleTimeString() },
    { key: "camera_id", label: "Camera", render: (d) => cameras[d.camera_id]?.camera_code || d.camera_id },
    { key: "cls", label: "Class" },
    { key: "track_id", label: "Track ID", render: (d) => d.track_id ?? "—" },
    { key: "confidence", label: "Confidence", render: (d) => `${(d.confidence * 100).toFixed(0)}%` },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">AI Vision</h1>
      {!error && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <KpiCard title="Persons Detected" value={personCount} />
          <KpiCard title="Vehicles Detected" value={vehicleCount} />
          <KpiCard title="Active Tracks" value={new Set(detectionRows.map((d) => d.track_id).filter(Boolean)).size} />
          <KpiCard title="Total Events" value={detectionRows.length} />
        </div>
      )}
      <div className="flex gap-2">
        {CLASSES.map((c) => (
          <button
            key={c}
            onClick={() => setCls(c)}
            className={`text-xs px-3 py-1.5 rounded border ${cls === c ? "border-accent text-accent" : "border-border text-slate-400"}`}
          >
            {c.toUpperCase()}
          </button>
        ))}
      </div>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable columns={columns} rows={detectionRows} emptyTitle="No detections yet" emptyHint="Add a camera to start the real detection pipeline." />
      )}
    </div>
  );
}

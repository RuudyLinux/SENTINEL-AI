"use client";
import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import SeverityBadge from "@/components/SeverityBadge";
import ErrorState from "@/components/ErrorState";
import { useLiveSocket } from "@/lib/useLiveSocket";

const SEVERITIES = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

export default function AlertsPage() {
  const router = useRouter();
  const params = useSearchParams();
  const [severity, setSeverity] = useState("ALL");

  const alertsPath = severity === "ALL" ? "/api/alerts" : `/api/alerts?severity=${severity}`;
  const { data: alertsData, error, reload } = useApiData<any[]>(alertsPath, { pollMs: 5000 });
  const alerts = alertsData || [];

  const { data: camsData } = useApiData<any[]>("/api/cameras");
  const cameras = useMemo(() => Object.fromEntries((camsData || []).map((c) => [c.id, c])), [camsData]);

  useLiveSocket((e) => {
    if (e.type === "alert") reload();
  });

  const cameraFilter = params.get("camera_id");
  const filtered = cameraFilter ? alerts.filter((a) => a.camera_id === cameraFilter) : alerts;

  const summary = SEVERITIES.slice(1).map((s) => ({ s, count: alerts.filter((a) => a.severity === s && a.status === "new").length }));

  const columns: Column<any>[] = [
    { key: "severity", label: "Severity", render: (a) => <SeverityBadge severity={a.severity} /> },
    { key: "camera_id", label: "Camera", render: (a) => cameras[a.camera_id]?.camera_code || a.camera_id },
    { key: "reasons", label: "Why Triggered", render: (a) => a.reasons.join("; ") },
    { key: "confidence", label: "Confidence", render: (a) => `${(a.confidence * 100).toFixed(0)}%` },
    { key: "status", label: "Status" },
    { key: "timestamp", label: "Time", render: (a) => new Date(a.timestamp).toLocaleTimeString() },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Alert Center</h1>
      <div className="flex gap-4">
        {summary.map(({ s, count }) => (
          <div key={s} className="text-center">
            <div className={`text-2xl font-semibold severity-${s}`}>{error ? "—" : count}</div>
            <div className="text-[10px] text-slate-500 uppercase">{s}</div>
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        {SEVERITIES.map((s) => (
          <button key={s} onClick={() => setSeverity(s)} className={`text-xs px-3 py-1.5 rounded border ${severity === s ? "border-accent text-accent" : "border-border text-slate-400"}`}>
            {s}
          </button>
        ))}
      </div>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable columns={columns} rows={filtered} onRowClick={(a) => router.push(`/alerts/${a.id}`)} emptyTitle="No alerts" />
      )}
    </div>
  );
}

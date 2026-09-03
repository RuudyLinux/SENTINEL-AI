"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";

export default function VehiclesHubPage() {
  const router = useRouter();
  const { data: vehicles, error, reload } = useApiData<any[]>("/api/vehicles");

  const columns: Column<any>[] = [
    { key: "plate_text", label: "Plate", render: (v) => v.plate_text || "Unread" },
    { key: "plate_confidence", label: "OCR Confidence", render: (v) => `${(v.plate_confidence * 100).toFixed(0)}%` },
    { key: "watchlist_flag", label: "Watchlist", render: (v) => (v.watchlist_flag ? "⚠ MATCH" : "—") },
    { key: "first_seen", label: "First Seen", render: (v) => new Date(v.first_seen).toLocaleString() },
    { key: "last_seen", label: "Last Seen", render: (v) => new Date(v.last_seen).toLocaleString() },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Vehicle Intelligence</h1>
        <div className="flex gap-2">
          <Link href="/vehicles/anpr" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">ANPR</Link>
          <Link href="/vehicles/tracking" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">TRACKING</Link>
          <Link href="/watchlists" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">WATCHLIST</Link>
        </div>
      </div>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <DataTable
          columns={columns}
          rows={vehicles || []}
          onRowClick={(v) => router.push(`/vehicles/tracking?vehicle_id=${v.id}`)}
          emptyTitle="No vehicles recorded yet"
          emptyHint="Vehicles appear once ANPR reads a legible plate from a real camera feed."
        />
      )}
    </div>
  );
}

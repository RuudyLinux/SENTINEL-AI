"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";

export default function AnprPage() {
  const router = useRouter();
  const [plate, setPlate] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const vehicles = await api.get<any[]>(`/api/vehicles?plate=${encodeURIComponent(plate)}`);
      setResults(vehicles);
      setSearched(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Search request failed");
    }
  }

  const columns: Column<any>[] = [
    { key: "plate_text", label: "Plate" },
    { key: "vehicle_type", label: "Type", render: (v) => v.vehicle_type || "—" },
    { key: "plate_confidence", label: "Confidence", render: (v) => `${(v.plate_confidence * 100).toFixed(0)}%` },
    { key: "first_seen", label: "First Seen", render: (v) => new Date(v.first_seen).toLocaleTimeString() },
    { key: "last_seen", label: "Last Seen", render: (v) => new Date(v.last_seen).toLocaleTimeString() },
    { key: "watchlist_flag", label: "Watchlist", render: (v) => (v.watchlist_flag ? "⚠ POTENTIAL MATCH" : "—") },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">ANPR — Automatic Number Plate Recognition</h1>
      <form onSubmit={search} className="flex gap-2 max-w-md">
        <input
          value={plate}
          onChange={(e) => setPlate(e.target.value.toUpperCase())}
          placeholder="GJ05AB1234"
          className="flex-1 bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent font-mono"
        />
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">SEARCH</button>
      </form>
      {error ? (
        <ErrorState message={error} onRetry={() => search({ preventDefault: () => {} } as React.FormEvent)} />
      ) : (
        <DataTable
          columns={columns}
          rows={results}
          onRowClick={(v) => router.push(`/vehicles/tracking?vehicle_id=${v.id}`)}
          emptyTitle={searched ? "No plate matches found" : "Search a plate to see results"}
          emptyHint="Reads come from real OCR over vehicle crops — accuracy depends on plate visibility in the source footage."
        />
      )}
    </div>
  );
}

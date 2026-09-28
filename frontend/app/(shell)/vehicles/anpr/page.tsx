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
      // exact plate goes straight to that vehicle, no point making the officer
      // click through a one-result list. partial matches still get the list
      // empty box lists everything; /by-plate/ with no plate is always a 404
      if (plate.trim()) {
        try {
          const exact = await api.get<any>(`/api/vehicles/by-plate/${encodeURIComponent(plate.trim())}`);
          router.push(`/vehicles/${exact.id}`);
          return;
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 404) throw err;
        }
      }
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
          aria-label="Plate number"
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
          onRowClick={(v) => router.push(`/vehicles/${v.id}`)}
          emptyTitle={searched ? "No plate matches found" : "Search a plate to see results"}
          emptyHint="Reads come from real OCR over vehicle crops — accuracy depends on plate visibility in the source footage."
        />
      )}
    </div>
  );
}

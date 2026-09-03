"use client";
import Link from "next/link";
import { useApiData } from "@/lib/useApiData";
import LiveVideoTile from "@/components/LiveVideoTile";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";

export default function LiveCamerasPage() {
  const { data: cameras, error, reload } = useApiData<any[]>("/api/cameras", { pollMs: 5000 });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Live Cameras</h1>
        <div className="flex gap-2">
          <Link href="/live/map" className="text-xs border border-border rounded px-3 py-1.5 hover:border-accent">MAP VIEW</Link>
          <Link href="/cameras/add" className="text-xs bg-accent text-ink font-medium rounded px-3 py-1.5">ADD CAMERA</Link>
        </div>
      </div>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : !cameras || cameras.length === 0 ? (
        <EmptyState title="No cameras onboarded" hint="Add a webcam or upload a video file to start the real detection pipeline." />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {cameras.map((c) => <LiveVideoTile key={c.id} camera={c} />)}
        </div>
      )}
    </div>
  );
}

"use client";
import dynamic from "next/dynamic";
import { useApiData } from "@/lib/useApiData";
import ErrorState from "@/components/ErrorState";

const CameraMap = dynamic(() => import("@/components/CameraMap"), { ssr: false });

export default function LiveMapPage() {
  const { data: cameras, error, reload } = useApiData<any[]>("/api/cameras");

  return (
    <div className="space-y-4 h-full flex flex-col">
      <h1 className="text-lg font-semibold">Camera Network Map</h1>
      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <div className="flex-1 min-h-[500px] border border-border rounded-lg overflow-hidden">
          <CameraMap cameras={cameras || []} />
        </div>
      )}
    </div>
  );
}

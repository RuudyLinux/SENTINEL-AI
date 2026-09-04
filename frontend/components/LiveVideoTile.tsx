"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { buildTokenedUrl } from "@/lib/api";
import StatusDot from "./StatusDot";

export default function LiveVideoTile({ camera }: { camera: any }) {
  const [streamUrl, setStreamUrl] = useState<string | null>(null);

  useEffect(() => {
    if (camera.status !== "online") return;
    let cancelled = false;
    buildTokenedUrl(`/api/streams/${camera.id}/stream-token`, `/api/streams/${camera.id}/mjpeg`)
      .then((url) => { if (!cancelled) setStreamUrl(url); })
      .catch(() => { if (!cancelled) setStreamUrl(null); });
    return () => { cancelled = true; };
  }, [camera.status, camera.id]);

  return (
    <div className="border border-border rounded-lg overflow-hidden bg-panel flex flex-col">
      <div className="flex items-center justify-between px-2 py-1 text-xs bg-panel2">
        <StatusDot status={camera.status} />
        <span className="font-mono">{camera.camera_code}</span>
      </div>
      <div className="aspect-video bg-black flex items-center justify-center">
        {camera.status === "online" && streamUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={streamUrl}
            alt={camera.name}
            className="w-full h-full object-cover"
          />
        ) : (
          <div className="text-xs text-slate-500 text-center px-4">
            {camera.status === "offline" ? "Camera offline" : "Connecting..."}
          </div>
        )}
      </div>
      <div className="px-2 py-1.5 text-xs flex items-center justify-between text-slate-400">
        <span>{camera.location} | {camera.resolution || "—"} | {camera.fps ? camera.fps.toFixed(0) : 0} FPS</span>
      </div>
      <div className="px-2 pb-2 flex gap-2">
        <Link href={`/live/${camera.id}`} className="flex-1 text-center text-xs bg-panel2 border border-border rounded py-1 hover:border-accent">
          VIEW
        </Link>
      </div>
    </div>
  );
}

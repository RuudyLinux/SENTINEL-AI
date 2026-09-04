"use client";
import { useState } from "react";
import Link from "next/link";
import { buildTokenedUrl } from "@/lib/api";
import ConnectionBadge, { AiBadge, deriveConnectionState } from "./ConnectionBadge";

export default function LiveVideoTile({ camera }: { camera: any }) {
  // This grid is a management/overview surface, not an auto-playing wall —
  // with the 24/7 auto-connect supervisor now bringing several real cameras
  // online without any operator action, fetching every online camera's MJPEG
  // stream on mount would silently open that many browser video players the
  // moment this page loads. Preview is opt-in per tile; the single-camera
  // page (VIEW) is the actual "operator selected this camera" path.
  const [previewing, setPreviewing] = useState(false);
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const connectionState = deriveConnectionState(camera);
  const isLive = connectionState === "CONNECTED" || connectionState === "PROCESSING";

  function startPreview() {
    if (!isLive) return;
    setPreviewing(true);
    buildTokenedUrl(`/api/streams/${camera.id}/stream-token`, `/api/streams/${camera.id}/mjpeg`)
      .then((url) => setStreamUrl(url))
      .catch(() => setStreamUrl(null));
  }

  return (
    <div className="border border-border rounded-lg overflow-hidden bg-panel flex flex-col">
      <div className="flex items-center justify-between gap-1 px-2 py-1 text-xs bg-panel2 flex-wrap">
        <div className="flex items-center gap-1.5">
          <ConnectionBadge camera={camera} />
          <AiBadge camera={camera} />
        </div>
        <span className="font-mono">{camera.camera_code}</span>
      </div>
      <div className="aspect-video bg-black flex items-center justify-center">
        {previewing && streamUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={streamUrl} alt={camera.name} className="w-full h-full object-cover" />
        ) : isLive ? (
          <button onClick={startPreview} className="text-xs text-accent border border-accent/40 rounded px-3 py-1.5 hover:bg-accent/10">
            ▶ Preview
          </button>
        ) : (
          <div className="text-xs text-slate-500 text-center px-4">
            {connectionState === "REGISTERED" ? "Registered — not connected" : connectionState.replace("_", " ")}
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

"use client";
import { useState } from "react";
import Link from "next/link";
import { Play, Eye } from "lucide-react";
import { useStreamUrl } from "@/lib/useStreamUrl";
import ConnectionBadge, { AiBadge, deriveConnectionState } from "./ConnectionBadge";
import { RecIndicator } from "./RecButton";

export default function LiveVideoTile({ camera }: { camera: any }) {
  // An overview grid, not a video wall. With the supervisor bringing cameras
  // online by itself, opening every MJPEG on mount would start that many
  // players the moment the page loads. Preview is opt-in per tile; VIEW is
  // the single-camera page.
  const [previewing, setPreviewing] = useState(false);
  const connectionState = deriveConnectionState(camera);
  const isLive = connectionState === "CONNECTED" || connectionState === "PROCESSING";
  // re-authorized while the preview is open, the backend cuts streams at
  // token expiry and the tile would freeze
  const streamUrl = useStreamUrl(
    `/api/streams/${camera.id}/stream-token`,
    `/api/streams/${camera.id}/mjpeg`,
    previewing && isLive,
  );

  function startPreview() {
    if (!isLive) return;
    setPreviewing(true);
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
      <div className="relative aspect-video bg-black flex items-center justify-center">
        <RecIndicator recording={!!camera.recording} />
        {previewing && streamUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={streamUrl} alt={camera.name} className="w-full h-full object-cover" />
        ) : isLive ? (
          <button
            onClick={startPreview}
            className="flex items-center gap-1.5 text-xs text-accent border border-accent/40 rounded px-3 py-1.5 hover:bg-accent/10 transition-colors duration-150"
          >
            <Play size={13} strokeWidth={2.25} />
            Preview
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
        <Link
          href={`/live/${camera.id}`}
          className="flex-1 flex items-center justify-center gap-1.5 text-xs bg-panel2 border border-border rounded py-1 hover:border-accent transition-colors duration-150"
        >
          <Eye size={13} strokeWidth={2.25} />
          VIEW
        </Link>
      </div>
    </div>
  );
}

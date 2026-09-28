"use client";
import { useEffect, useRef, useState } from "react";
import { playWhep } from "@/lib/whep";

/** Live WebRTC video from a camera's WHEP URL. Raw camera video: the AI boxes
 * are drawn only on the backend's MJPEG stream, so the caller offers both.
 * Reports failure through onError so the caller can fall back to MJPEG. */
export default function WhepVideo({ url, label, onError }: { url: string; label: string; onError: (message: string) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    const el = video.current;
    if (!el) return;
    const abort = new AbortController();
    let stop: (() => void) | null = null;
    let cancelled = false;
    // No first frame within this long means the stream is not coming.
    const watchdog = setTimeout(() => { if (!cancelled) onError("No video from the WebRTC stream within 10 s"); }, 10_000);
    const onPlaying = () => { clearTimeout(watchdog); setPlaying(true); };
    el.addEventListener("playing", onPlaying);
    playWhep(url, el, abort.signal)
      .then((s) => { if (cancelled) s(); else stop = s; })
      .catch((err) => { if (!cancelled) { clearTimeout(watchdog); onError(err?.message || "WebRTC connection failed"); } });
    return () => {
      cancelled = true;
      clearTimeout(watchdog);
      abort.abort();
      el.removeEventListener("playing", onPlaying);
      stop?.();
      el.srcObject = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  return (
    <div className="relative w-full h-full">
      <video ref={video} autoPlay muted playsInline aria-label={label} className="w-full h-full object-contain" />
      {!playing && <div className="absolute inset-0 flex items-center justify-center text-slate-500 text-sm">Connecting WebRTC…</div>}
    </div>
  );
}

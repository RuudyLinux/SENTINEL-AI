"use client";
import { useEffect, useRef, useState } from "react";

/** Smart Shield brand mark — falls back to the project's existing shield
 * icon (app/icon.svg, always present) until the real logo is placed at
 * public/branding/smart-shield-logo.png (see that folder's README).
 *
 * Real bug found via live browser testing, not just reading the code: a
 * plain `<img onError={...}>` misses the fallback on a fast (e.g.
 * localhost) 404 — the native `error` event can fire before React finishes
 * hydrating and attaches its synthetic listener, so `onError` never runs
 * and the broken image just sits there. Checked directly: naturalWidth was
 * 0 and `/icon.svg` never got requested. Fixed by also checking
 * `img.complete` on mount (catches an error that already happened before
 * hydration) in addition to the `onError` handler (catches one that
 * happens after). Shared here so both the login page and the sidebar use
 * the same, actually-verified-working fallback instead of duplicating it.
 */
export default function BrandLogo({ size, className = "" }: { size: number; className?: string }) {
  const [src, setSrc] = useState("/branding/smart-shield-logo.png");
  const imgRef = useRef<HTMLImageElement>(null);
  const fellBack = useRef(false);

  function fallback() {
    if (fellBack.current) return;
    fellBack.current = true;
    setSrc("/icon.svg");
  }

  useEffect(() => {
    const img = imgRef.current;
    if (img && img.complete && img.naturalWidth === 0) fallback();
  }, []);

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      ref={imgRef}
      src={src}
      onError={fallback}
      alt="Smart Shield — Gujarat Police Innovation Challenge 2026"
      className={`object-contain ${className}`}
      style={{ height: size, width: size }}
    />
  );
}

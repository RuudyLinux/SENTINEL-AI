"use client";
import { useEffect, useRef, useState } from "react";

/** Smart Shield brand mark, with the project's own shield icon
 * (app/icon.svg, always present) as both the default and the safety net.
 *
 * The default used to be `/branding/smart-shield-logo.png`, a file the repo
 * does not contain. The fallback below worked, so the mark rendered
 * correctly — but every page load in every browser still fetched a missing
 * file and logged `404 (Not Found)` for it. A 404 on every page teaches
 * whoever reads that console to ignore 404s, which is the actual cost.
 *
 * The real logo is opt-in now: set NEXT_PUBLIC_BRAND_LOGO_URL (see
 * public/branding/README.md). Unset — the shipped state — nothing is
 * requested that does not exist.
 *
 * The fallback stays, because an override can still point at a missing or
 * broken file. Real bug found via live browser testing, not just reading the
 * code: a plain `<img onError={...}>` misses the fallback on a fast (e.g.
 * localhost) 404 — the native `error` event can fire before React finishes
 * hydrating and attaches its synthetic listener, so `onError` never runs
 * and the broken image just sits there. Checked directly: naturalWidth was
 * 0 and `/icon.svg` never got requested. Fixed by also checking
 * `img.complete` on mount (catches an error that already happened before
 * hydration) in addition to the `onError` handler (catches one that
 * happens after). Shared here so both the login page and the sidebar use
 * the same, actually-verified-working fallback instead of duplicating it.
 */
const FALLBACK_LOGO = "/icon.svg";
/** Read at module scope: NEXT_PUBLIC_* is inlined at build time, so this is
 *  a constant in the bundle, not a per-render environment lookup. */
const BRAND_LOGO_URL = process.env.NEXT_PUBLIC_BRAND_LOGO_URL || FALLBACK_LOGO;

export default function BrandLogo({ size, className = "" }: { size: number; className?: string }) {
  const [src, setSrc] = useState(BRAND_LOGO_URL);
  const imgRef = useRef<HTMLImageElement>(null);
  const fellBack = useRef(false);

  function fallback() {
    if (fellBack.current) return;
    fellBack.current = true;
    setSrc(FALLBACK_LOGO);
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

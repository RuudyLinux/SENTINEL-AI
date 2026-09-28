"use client";
import { useEffect, useRef, useState } from "react";

/** Smart Shield mark, falling back to the project's shield icon (app/icon.svg).
 *
 * A custom logo is opt-in via NEXT_PUBLIC_BRAND_LOGO_URL
 * (public/branding/README.md). img.complete is checked on mount as well as
 * onError, because a fast 404 can fire before hydration attaches the listener.
 */
const FALLBACK_LOGO = "/icon.svg";
/** NEXT_PUBLIC_* is inlined at build time. */
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

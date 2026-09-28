"use client";
import { useEffect, useRef, useState } from "react";

/** Smart Shield mark, with the project's shield icon (app/icon.svg, always
 * there) as default and fallback.
 *
 * The default used to be /branding/smart-shield-logo.png, which isn't in the
 * repo, so every page load logged a 404 even though the fallback rendered
 * fine. A console full of 404s teaches people to ignore 404s. The real logo is
 * opt-in via NEXT_PUBLIC_BRAND_LOGO_URL (public/branding/README.md).
 *
 * The fallback stays for a broken override. A plain <img onError> misses a
 * fast localhost 404: the native error fires before hydration attaches the
 * listener, so onError never runs (naturalWidth 0, /icon.svg never
 * requested). So we also check img.complete on mount. Shared by login and
 * sidebar.
 */
const FALLBACK_LOGO = "/icon.svg";
/** Module scope: NEXT_PUBLIC_* is inlined at build time, it's a constant. */
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

import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ink: "#0b0f14",
        panel: "#111823",
        panel2: "#161f2c",
        border: "#22303f",
        accent: "#2dd4bf",
        critical: "#ef4444",
        high: "#f97316",
        medium: "#eab308",
        low: "#3b82f6",
        ok: "#22c55e",
        // Smart Shield brand accent (Gujarat Police Innovation Challenge
        // 2026) — used SELECTIVELY (login badge, brand mark ring), never a
        // wholesale recolor. The existing ink/panel palette is already deep
        // navy/blue, which is most of the Smart Shield identity; this adds
        // just the orange note that isn't otherwise in the palette.
        "brand-orange": "#f97316",
      },
      // Global animation language (Phase 7): a handful of named durations/
      // keyframes reused everywhere instead of ad-hoc values per component.
      transitionDuration: {
        fast: "150ms",
        normal: "250ms",
        medium: "300ms",
      },
      keyframes: {
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "slide-up": { from: { opacity: "0", transform: "translateY(10px)" }, to: { opacity: "1", transform: "translateY(0)" } },
        "scale-in": { from: { opacity: "0", transform: "scale(0.97)" }, to: { opacity: "1", transform: "scale(1)" } },
        "dropdown-in": { from: { opacity: "0", transform: "translateY(-4px)" }, to: { opacity: "1", transform: "translateY(0)" } },
        "pulse-subtle": { "0%, 100%": { opacity: "1" }, "50%": { opacity: "0.55" } },
      },
      animation: {
        "fade-in": "fade-in 250ms ease-out both",
        "slide-up": "slide-up 300ms ease-out both",
        "scale-in": "scale-in 200ms ease-out both",
        "dropdown-in": "dropdown-in 150ms ease-out both",
        "pulse-subtle": "pulse-subtle 2.2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
export default config;

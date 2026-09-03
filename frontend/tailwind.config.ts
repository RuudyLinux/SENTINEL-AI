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
      },
    },
  },
  plugins: [],
};
export default config;

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SENTINEL VISION",
  description: "AI-Powered Unified Video Intelligence for Smart Policing",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-ink text-slate-100 antialiased">{children}</body>
    </html>
  );
}

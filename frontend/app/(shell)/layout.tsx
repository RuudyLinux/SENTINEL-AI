"use client";
import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import Header from "@/components/Header";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem("sentinel_token");
    if (!token) {
      router.replace("/login");
    } else {
      setReady(true);
    }
  }, [router]);

  // close the mobile drawer on every route change, so nav links don't each
  // need an onClick
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  // Escape closes the drawer, matching standard off-canvas-nav behavior.
  useEffect(() => {
    if (!navOpen) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setNavOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navOpen]);

  if (!ready) return null;

  return (
    <div className="flex min-h-screen">
      <Sidebar open={navOpen} onNavigate={() => setNavOpen(false)} />
      {navOpen && (
        // backdrop, mobile only; clicking closes the drawer (sidebar z-40 over
        // this z-30)
        <div
          className="fixed inset-0 bg-black/50 z-30 md:hidden"
          onClick={() => setNavOpen(false)}
          aria-hidden="true"
        />
      )}
      <div className="flex-1 flex flex-col min-w-0">
        <Header onMenuClick={() => setNavOpen((v) => !v)} />
        <main className="flex-1 p-3 sm:p-5 min-w-0 overflow-x-hidden">{children}</main>
      </div>
    </div>
  );
}

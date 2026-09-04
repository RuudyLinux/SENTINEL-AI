"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Menu, Search, Bell, Wifi, WifiOff, CircleUserRound, LogOut } from "lucide-react";
import { clearToken, getStoredUser } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

export default function Header({ onMenuClick }: { onMenuClick?: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [user, setUser] = useState<any>(null);
  const { data: overview, error } = useApiData<any>("/api/analytics/overview", { pollMs: 15000 });

  useEffect(() => {
    setUser(getStoredUser());
  }, []);

  function submitSearch(e: React.FormEvent) {
    e.preventDefault();
    if (q.trim()) router.push(`/search?q=${encodeURIComponent(q.trim())}`);
  }

  function logout() {
    clearToken();
    router.push("/login");
  }

  return (
    <header className="h-14 border-b border-border bg-panel flex items-center gap-2 sm:gap-4 px-2 sm:px-4 sticky top-0 z-10">
      <button
        onClick={onMenuClick}
        aria-label="Open navigation menu"
        className="md:hidden shrink-0 text-slate-300 hover:text-accent px-1.5 py-1.5 border border-border rounded-md transition-colors duration-150"
      >
        <Menu size={18} strokeWidth={2} />
      </button>
      <form onSubmit={submitSearch} className="flex-1 min-w-0 sm:max-w-xl relative">
        <Search size={15} strokeWidth={2} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" aria-hidden="true" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search camera, person, vehicle, plate..."
          className="w-full min-w-0 bg-panel2 border border-border rounded-md pl-9 pr-3 py-1.5 text-sm outline-none focus:border-accent transition-colors duration-150"
        />
      </form>
      <div className="flex items-center gap-2 sm:gap-4 ml-auto text-sm shrink-0">
        <button
          onClick={() => router.push("/alerts")}
          className="relative flex items-center gap-1.5 text-slate-300 hover:text-accent transition-colors duration-150"
          title={error ? "Alert count unavailable — backend unreachable" : undefined}
        >
          <Bell size={17} strokeWidth={2} />
          <span className="text-xs">{error ? "—" : overview?.alerts.active ?? 0}</span>
        </button>
        {/* System status — the one continuously-animated element in the navbar
            (a subtle breathing pulse on the dot only), per the "don't animate
            the whole navbar" rule. */}
        <span
          className={`hidden sm:flex items-center gap-1.5 text-xs ${error ? "text-critical" : "text-slate-400"}`}
          title={error ? `Backend unreachable: ${error}` : "Backend reachable"}
        >
          <span className="relative flex h-2 w-2">
            <span className={`absolute inline-flex h-full w-full rounded-full ${error ? "bg-critical" : "bg-ok"} ${error ? "" : "animate-pulse-subtle"}`} />
          </span>
          {error ? <WifiOff size={14} strokeWidth={2} /> : <Wifi size={14} strokeWidth={2} />}
          {error ? "System unreachable" : "System online"}
        </span>
        {user && (
          <div className="flex items-center gap-2">
            <CircleUserRound size={20} strokeWidth={1.75} className="hidden md:block text-slate-400" aria-hidden="true" />
            <div className="hidden md:block text-right leading-tight">
              <div className="text-xs font-medium">{user.full_name}</div>
              <div className="text-[10px] text-slate-500">{user.role}</div>
            </div>
            <button
              onClick={logout}
              className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-red-400 border border-border rounded px-2 py-1 shrink-0 transition-colors duration-150"
            >
              <LogOut size={13} strokeWidth={2} />
              Logout
            </button>
          </div>
        )}
      </div>
    </header>
  );
}

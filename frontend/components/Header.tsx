"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { clearToken, getStoredUser } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

export default function Header() {
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
    <header className="h-14 border-b border-border bg-panel flex items-center gap-4 px-4 sticky top-0 z-10">
      <form onSubmit={submitSearch} className="flex-1 max-w-xl">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search camera, person, vehicle, plate, incident, event..."
          className="w-full bg-panel2 border border-border rounded-md px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
      </form>
      <div className="flex items-center gap-4 ml-auto text-sm">
        <button onClick={() => router.push("/alerts")} className="relative text-slate-300 hover:text-accent" title={error ? "Alert count unavailable — backend unreachable" : undefined}>
          🔔 {error ? "—" : overview?.alerts.active ?? 0}
        </button>
        <span
          className={`flex items-center gap-1.5 text-xs ${error ? "text-critical" : "text-slate-400"}`}
          title={error ? `Backend unreachable: ${error}` : "Backend reachable"}
        >
          <span className={`h-2 w-2 rounded-full ${error ? "bg-critical" : "bg-ok"}`} />
          {error ? "System unreachable" : "System"}
        </span>
        {user && (
          <div className="flex items-center gap-2">
            <div className="text-right leading-tight">
              <div className="text-xs font-medium">{user.full_name}</div>
              <div className="text-[10px] text-slate-500">{user.role}</div>
            </div>
            <button onClick={logout} className="text-xs text-slate-400 hover:text-red-400 border border-border rounded px-2 py-1">
              Logout
            </button>
          </div>
        )}
      </div>
    </header>
  );
}

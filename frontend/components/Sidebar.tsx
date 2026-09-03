"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const PRIMARY = [
  { href: "/dashboard", label: "Command Center" },
  { href: "/live", label: "Live Cameras" },
  { href: "/vision", label: "AI Vision" },
  { href: "/search", label: "Search" },
  { href: "/alerts", label: "Alerts" },
  { href: "/incidents", label: "Incidents" },
  { href: "/investigate", label: "Investigate" },
  { href: "/map", label: "Map" },
  { href: "/evidence", label: "Evidence" },
  { href: "/analytics", label: "Analytics" },
];

const ADMIN = [
  { href: "/cameras", label: "Cameras" },
  { href: "/watchlists", label: "Watchlists" },
  { href: "/admin/rules", label: "AI Rules" },
  { href: "/admin/users", label: "Users" },
  { href: "/admin/audit", label: "Audit" },
  { href: "/admin/system", label: "System" },
];

function NavGroup({ items, pathname }: { items: typeof PRIMARY; pathname: string }) {
  return (
    <>
      {items.map((item) => {
        const active = pathname === item.href || pathname.startsWith(item.href + "/");
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`block px-3 py-2 rounded text-sm ${
              active ? "bg-accent/10 text-accent font-medium" : "text-slate-300 hover:bg-panel2"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </>
  );
}

export default function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="w-56 shrink-0 border-r border-border bg-panel h-screen sticky top-0 flex flex-col">
      <div className="px-4 py-4 border-b border-border">
        <div className="text-sm font-bold tracking-wide text-slate-100">SENTINEL VISION</div>
        <div className="text-[10px] text-slate-500 mt-0.5">Unified Video Intelligence</div>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-0.5">
        <NavGroup items={PRIMARY} pathname={pathname} />
        <div className="pt-3 mt-3 border-t border-border text-[10px] uppercase tracking-wide text-slate-500 px-3">
          Administration
        </div>
        <NavGroup items={ADMIN} pathname={pathname} />
      </nav>
    </aside>
  );
}

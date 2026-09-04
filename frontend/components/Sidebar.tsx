"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ICONS } from "@/lib/navIcons";
import BrandLogo from "@/components/BrandLogo";

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
        const Icon = NAV_ICONS[item.href];
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`group relative flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors duration-150 ${
              active ? "bg-accent/10 text-accent font-medium" : "text-slate-300 hover:bg-panel2 hover:text-slate-100"
            }`}
          >
            {/* Active indicator — a static left accent bar, not a moving/animated
                highlight, so it reads instantly without drawing attention to itself. */}
            <span
              className={`absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-accent transition-opacity duration-150 ${
                active ? "opacity-100" : "opacity-0"
              }`}
              aria-hidden="true"
            />
            {Icon && <Icon size={16} strokeWidth={2} className="shrink-0" aria-hidden="true" />}
            <span className="truncate">{item.label}</span>
          </Link>
        );
      })}
    </>
  );
}

export default function Sidebar({ open = false, onNavigate }: { open?: boolean; onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <aside
      className={`w-56 shrink-0 border-r border-border bg-panel h-screen flex flex-col z-40
        fixed inset-y-0 left-0 transform transition-transform duration-200 ease-out
        ${open ? "translate-x-0" : "-translate-x-full"}
        md:translate-x-0 md:sticky md:top-0`}
    >
      <div className="px-4 py-4 border-b border-border flex items-center gap-2.5">
        <BrandLogo size={32} className="rounded shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-bold tracking-wide text-slate-100 truncate">SENTINEL VISION</div>
          <div className="text-[10px] text-slate-500 mt-0.5 truncate">Smart Shield · Unified Video Intelligence</div>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-0.5" onClick={onNavigate}>
        <NavGroup items={PRIMARY} pathname={pathname} />
        <div className="pt-3 mt-3 border-t border-border text-[10px] uppercase tracking-wide text-slate-500 px-3">
          Administration
        </div>
        <NavGroup items={ADMIN} pathname={pathname} />
      </nav>
    </aside>
  );
}

// Single source of truth for "which icon means which nav destination" —
// Sidebar (desktop + mobile drawer, same component) is the only consumer,
// but kept here rather than inline so the mapping can't drift if anything
// else ever needs the same icon-per-route semantics (e.g. a future
// breadcrumb or command palette). Matches the actual nav labels/routes in
// Sidebar.tsx — none renamed, none invented.
import {
  LayoutDashboard, Camera, ScanLine, Search, Bell, ShieldAlert, FileSearch,
  Map, FolderLock, BarChart3, ListChecks, SlidersHorizontal, Users,
  ScrollText, Settings, type LucideIcon,
} from "lucide-react";

export const NAV_ICONS: Record<string, LucideIcon> = {
  "/dashboard": LayoutDashboard,       // Command Center
  "/live": Camera,                     // Live Cameras
  "/vision": ScanLine,                 // AI Vision (live detection)
  "/search": Search,
  "/alerts": Bell,
  "/incidents": ShieldAlert,
  "/investigate": FileSearch,
  "/map": Map,
  "/evidence": FolderLock,
  "/analytics": BarChart3,
  "/cameras": Camera,
  "/watchlists": ListChecks,
  "/admin/rules": SlidersHorizontal,   // AI Rules (thresholds/filters)
  "/admin/users": Users,
  "/admin/audit": ScrollText,
  "/admin/system": Settings,
};

// nav destination -> icon. Sidebar is the only user, kept here anyway so it
// can't drift if something else needs it. Labels/routes match Sidebar.tsx.
import {
  LayoutDashboard, Camera, ScanLine, Search, Bell, ShieldAlert, FileSearch,
  Map, FolderLock, BarChart3, ListChecks, SlidersHorizontal, Users,
  ScrollText, Settings, Sliders, HeartPulse, TriangleAlert, Activity,
  Stethoscope, ClipboardList, type LucideIcon,
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
  "/cameras/control": Sliders,         // Camera Control Center
  "/watchlists": ListChecks,
  "/admin/rules": SlidersHorizontal,   // AI Rules (thresholds/filters)
  "/admin/users": Users,
  "/admin/audit": ScrollText,
  "/admin/system": Settings,
  "/self-heal/health": HeartPulse,
  "/self-heal/problems": TriangleAlert,
  "/self-heal/activity": Activity,
  "/self-heal/camera-health": Stethoscope,
  "/self-heal/errors": ClipboardList,
};

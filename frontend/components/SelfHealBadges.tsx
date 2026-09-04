import { Info, TriangleAlert, OctagonAlert, CheckCircle2, XCircle, Loader2, Settings2, type LucideIcon } from "lucide-react";

// Self-Heal severities (info | warning | critical) are a distinct, smaller
// set from alert severities (CRITICAL/HIGH/MEDIUM/LOW — see SeverityBadge)
// — a separate small component rather than overloading that one with a
// second, unrelated color/icon mapping.
const SEVERITY_COLORS: Record<string, string> = {
  critical: "bg-critical/15 text-critical border border-critical/30",
  warning: "bg-high/15 text-high border border-high/30",
  info: "bg-low/15 text-low border border-low/30",
};
const SEVERITY_ICONS: Record<string, LucideIcon> = { critical: OctagonAlert, warning: TriangleAlert, info: Info };

export function SelfHealSeverityBadge({ severity }: { severity: string }) {
  const key = (severity || "").toLowerCase();
  const Icon = SEVERITY_ICONS[key] || Info;
  return (
    <span className={`badge ${SEVERITY_COLORS[key] || "bg-slate-500/15 text-slate-400 border border-slate-500/30"}`}>
      <Icon size={11} strokeWidth={2.5} aria-hidden="true" />
      {severity?.toUpperCase()}
    </span>
  );
}

const STATUS_COLORS: Record<string, string> = {
  RECOVERED: "bg-ok/15 text-ok border border-ok/30",
  RECOVERING: "bg-low/15 text-low border border-low/30",
  FAILED: "bg-critical/15 text-critical border border-critical/30",
  CONFIG_REQUIRED: "bg-high/15 text-high border border-high/30",
};
const STATUS_ICONS: Record<string, LucideIcon> = {
  RECOVERED: CheckCircle2, RECOVERING: Loader2, FAILED: XCircle, CONFIG_REQUIRED: Settings2,
};

export function SelfHealStatusBadge({ status }: { status: string }) {
  const Icon = STATUS_ICONS[status] || Info;
  return (
    <span className={`badge ${STATUS_COLORS[status] || "bg-slate-500/15 text-slate-400 border border-slate-500/30"}`}>
      <Icon size={11} strokeWidth={2.5} className={status === "RECOVERING" ? "animate-spin" : ""} aria-hidden="true" />
      {status?.replace(/_/g, " ")}
    </span>
  );
}

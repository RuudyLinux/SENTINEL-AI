const COLORS: Record<string, string> = {
  CRITICAL: "bg-red-500/15 text-red-400 border border-red-500/30",
  HIGH: "bg-orange-500/15 text-orange-400 border border-orange-500/30",
  MEDIUM: "bg-yellow-500/15 text-yellow-400 border border-yellow-500/30",
  LOW: "bg-blue-500/15 text-blue-400 border border-blue-500/30",
};

export default function SeverityBadge({ severity }: { severity: string }) {
  const cls = COLORS[severity?.toUpperCase()] || "bg-slate-500/15 text-slate-400 border border-slate-500/30";
  return <span className={`badge ${cls}`}>{severity}</span>;
}

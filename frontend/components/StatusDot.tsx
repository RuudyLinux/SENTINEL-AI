const COLORS: Record<string, string> = {
  online: "bg-ok",
  operational: "bg-ok",
  offline: "bg-slate-500",
  degraded: "bg-high",
  new: "bg-critical",
};

export default function StatusDot({ status }: { status: string }) {
  const color = COLORS[status?.toLowerCase()] || "bg-slate-500";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {status}
    </span>
  );
}

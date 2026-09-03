export default function KpiCard({
  title, value, sub, onClick, buttonLabel,
}: { title: string; value: string | number; sub?: string; onClick?: () => void; buttonLabel?: string }) {
  return (
    <div className="rounded-lg border border-border bg-panel p-4 flex flex-col gap-2 min-w-[180px]">
      <div className="text-xs uppercase tracking-wide text-slate-400">{title}</div>
      <div className="text-3xl font-semibold text-slate-100">{value}</div>
      {sub && <div className="text-xs text-slate-400">{sub}</div>}
      {onClick && (
        <button onClick={onClick} className="mt-2 self-start text-xs font-medium text-accent hover:underline">
          {buttonLabel || "VIEW"}
        </button>
      )}
    </div>
  );
}

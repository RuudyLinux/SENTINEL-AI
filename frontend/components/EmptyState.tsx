export default function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center text-slate-400 border border-dashed border-border rounded-lg">
      <div className="text-sm font-medium text-slate-300">{title}</div>
      {hint && <div className="text-xs mt-1 max-w-sm">{hint}</div>}
    </div>
  );
}

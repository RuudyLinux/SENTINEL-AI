import { CircleAlert, RefreshCw } from "lucide-react";

/** Shown when a real fetch to the backend actually failed — never swapped
 * for placeholder/demo content. Distinct from EmptyState, which means
 * "the request succeeded and there is genuinely nothing to show yet".
 */
export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center border border-critical/30 bg-critical/5 rounded-lg gap-2 animate-fade-in">
      <CircleAlert size={22} strokeWidth={2} className="text-critical" aria-hidden="true" />
      <div className="text-sm font-medium text-critical">Data unavailable</div>
      <div className="text-xs max-w-sm text-slate-400">{message}</div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-1 flex items-center gap-1.5 text-xs border border-border rounded px-3 py-1.5 hover:border-accent transition-colors duration-150"
        >
          <RefreshCw size={12} strokeWidth={2.25} />
          RETRY
        </button>
      )}
    </div>
  );
}

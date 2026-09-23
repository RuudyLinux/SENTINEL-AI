import EmptyState from "./EmptyState";

export type Column<T> = {
  key: string;
  label: string;
  render?: (row: T) => React.ReactNode;
};

export default function DataTable<T extends { id: string }>({
  columns, rows, onRowClick, emptyTitle = "No results", emptyHint,
}: {
  columns: Column<T>[];
  rows: T[];
  onRowClick?: (row: T) => void;
  emptyTitle?: string;
  emptyHint?: string;
}) {
  if (!rows.length) return <EmptyState title={emptyTitle} hint={emptyHint} />;
  return (
    <div className="overflow-x-auto border border-border rounded-lg">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-panel2 text-slate-400 text-xs uppercase tracking-wide">
            {columns.map((c) => (
              <th key={c.key} className="text-left px-3 py-2 font-medium whitespace-nowrap">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              onClick={() => onRowClick?.(row)}
              className={`border-t border-border ${onRowClick ? "cursor-pointer hover:bg-panel2 transition-colors duration-150" : ""}`}
            >
              {columns.map((c) => {
                const value = c.render ? c.render(row) : (row as any)[c.key];
                return (
                  <td
                    key={c.key}
                    // `whitespace-nowrap` alone let one long value set the
                    // width of the whole table. Reproduced on the audit page:
                    // a single failed login carrying a 5,000-character
                    // username (unauthenticated input — see the matching bound
                    // in backend app/audit.py) rendered the table 36,215px
                    // wide, so every other column sat off-screen behind a
                    // horizontal scroll nobody would think to drag. The cap
                    // ellipsizes that cell instead of letting it deform the
                    // grid; the full value stays available on hover, and every
                    // ordinary cell is far below the cap and looks unchanged.
                    className="px-3 py-2 whitespace-nowrap max-w-xs truncate"
                    title={typeof value === "string" ? value : undefined}
                  >
                    {value}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

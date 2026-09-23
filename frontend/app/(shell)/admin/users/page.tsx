"use client";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";
import DataTable, { Column } from "@/components/DataTable";
import ErrorState from "@/components/ErrorState";

export default function UsersRolesPage() {
  const { data: usersData, error: usersError, reload: reloadUsers } = useApiData<any[]>("/api/users");
  const { data: rolesData, error: rolesError } = useApiData<any[]>("/api/roles");
  const users = usersData || [];
  const roles = rolesData || [];
  const [form, setForm] = useState({ username: "", password: "", full_name: "", department: "", role_name: "Control Room Operator" });
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Same double-submit guard as the other create forms. This one matters most:
  // a duplicate POST here either creates a second account or fails halfway
  // through, and an operator cannot tell which from a button that never
  // acknowledged the first click.
  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setActionError(null);
    setBusy(true);
    try {
      await api.post("/api/users", form);
      setForm({ username: "", password: "", full_name: "", department: "", role_name: "Control Room Operator" });
      reloadUsers();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create user");
    } finally {
      setBusy(false);
    }
  }

  async function setActive(id: string, active: boolean) {
    setActionError(null);
    try {
      await api.post(`/api/users/${id}/${active ? "enable" : "disable"}`);
      reloadUsers();
    } catch (err) {
      const fallback = active ? "Could not enable user" : "Could not disable user";
      setActionError(err instanceof ApiError ? err.message : fallback);
    }
  }

  const cols: Column<any>[] = [
    { key: "username", label: "Username" },
    { key: "full_name", label: "Full Name" },
    { key: "department", label: "Department" },
    { key: "role", label: "Role" },
    { key: "active", label: "Status", render: (u) => (u.active ? "Active" : "Disabled") },
    // A disabled account can now be restored: the API previously had no
    // enable route at all, so every disable here was permanent.
    {
      key: "actions",
      label: "Actions",
      render: (u) =>
        u.active ? (
          <button onClick={() => setActive(u.id, false)} className="text-xs text-slate-500 hover:text-critical">Disable</button>
        ) : (
          <button onClick={() => setActive(u.id, true)} className="text-xs text-slate-500 hover:text-accent">Enable</button>
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Users & Roles</h1>

      {/* htmlFor/id on every field, and an explicit autoComplete on each.
          Chrome logged "Input elements should have autocomplete attributes
          (suggested: current-password)" here on every visit — but
          `current-password` is the WRONG value and taking the suggestion
          would have introduced a real bug: this form creates SOMEONE ELSE'S
          account, so a password manager filling the signed-in administrator's
          own password into it is exactly what must not happen.
          `new-password` says that, and also stops the browser offering to
          save the new operator's password as the admin's. The unassociated
          labels were the same defect the login page already fixed: a screen
          reader announced five unlabelled boxes. */}
      <form onSubmit={create} className="bg-panel border border-border rounded-lg p-4 flex flex-wrap gap-2 items-end">
        <div><label htmlFor="new-user-username" className="text-xs text-slate-400">Username</label><input id="new-user-username" name="new-username" autoComplete="off" required value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label htmlFor="new-user-password" className="text-xs text-slate-400">Password</label><input id="new-user-password" name="new-password" autoComplete="new-password" required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label htmlFor="new-user-full-name" className="text-xs text-slate-400">Full name</label><input id="new-user-full-name" name="full_name" autoComplete="off" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label htmlFor="new-user-department" className="text-xs text-slate-400">Department</label><input id="new-user-department" name="department" autoComplete="off" value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div>
          <label htmlFor="new-user-role" className="text-xs text-slate-400">Role</label>
          <select id="new-user-role" name="role_name" value={form.role_name} onChange={(e) => setForm({ ...form, role_name: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
            {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
          </select>
          {rolesError && <div className="text-xs text-critical mt-1">Role list unavailable: {rolesError}</div>}
        </div>
        <button type="submit" disabled={busy} className="text-xs bg-accent text-ink font-medium rounded px-4 py-2 disabled:opacity-50">{busy ? "Creating..." : "CREATE USER"}</button>
      </form>
      {actionError && <div className="text-xs text-critical">{actionError}</div>}

      {usersError ? (
        <ErrorState message={usersError} onRetry={reloadUsers} />
      ) : (
        <DataTable columns={cols} rows={users} emptyTitle="No users" />
      )}
    </div>
  );
}

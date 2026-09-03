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

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setActionError(null);
    try {
      await api.post("/api/users", form);
      setForm({ username: "", password: "", full_name: "", department: "", role_name: "Control Room Operator" });
      reloadUsers();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create user");
    }
  }

  async function disable(id: string) {
    setActionError(null);
    try {
      await api.post(`/api/users/${id}/disable`);
      reloadUsers();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not disable user");
    }
  }

  const cols: Column<any>[] = [
    { key: "username", label: "Username" },
    { key: "full_name", label: "Full Name" },
    { key: "department", label: "Department" },
    { key: "role", label: "Role" },
    { key: "active", label: "Status", render: (u) => (u.active ? "Active" : "Disabled") },
    { key: "actions", label: "Actions", render: (u) => u.active && <button onClick={() => disable(u.id)} className="text-xs text-slate-500 hover:text-critical">Disable</button> },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Users & Roles</h1>

      <form onSubmit={create} className="bg-panel border border-border rounded-lg p-4 flex flex-wrap gap-2 items-end">
        <div><label className="text-xs text-slate-400">Username</label><input required value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label className="text-xs text-slate-400">Password</label><input required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label className="text-xs text-slate-400">Full name</label><input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div><label className="text-xs text-slate-400">Department</label><input value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1" /></div>
        <div>
          <label className="text-xs text-slate-400">Role</label>
          <select value={form.role_name} onChange={(e) => setForm({ ...form, role_name: e.target.value })} className="block bg-panel2 border border-border rounded px-3 py-2 text-sm mt-1">
            {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
          </select>
          {rolesError && <div className="text-xs text-critical mt-1">Role list unavailable: {rolesError}</div>}
        </div>
        <button className="text-xs bg-accent text-ink font-medium rounded px-4 py-2">CREATE USER</button>
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

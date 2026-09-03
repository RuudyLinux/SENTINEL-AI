"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken, setStoredUser, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [department, setDepartment] = useState("HQ");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.post<any>("/api/auth/login", { username, password, department });
      setToken(res.access_token);
      setStoredUser(res.user);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-ink">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="text-2xl font-bold tracking-wide">SENTINEL VISION</div>
          <div className="text-xs text-slate-400 mt-1">AI-Powered Unified Video Intelligence for Smart Policing</div>
        </div>
        <form onSubmit={onSubmit} className="bg-panel border border-border rounded-lg p-6 space-y-4">
          <div>
            <label className="text-xs text-slate-400">Police ID / Username</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent"
              required
            />
          </div>
          <div>
            <label className="text-xs text-slate-400">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent"
              required
            />
          </div>
          <div>
            <label className="text-xs text-slate-400">Department</label>
            <select
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent"
            >
              <option>HQ</option>
              <option>Ahmedabad</option>
              <option>Surat</option>
              <option>Vadodara</option>
              <option>Rajkot</option>
            </select>
          </div>
          {error && <div className="text-xs text-critical bg-red-500/10 border border-red-500/30 rounded px-3 py-2">{error}</div>}
          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent text-ink font-medium rounded-md py-2 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? "Signing in..." : "LOGIN"}
          </button>
          <div className="text-center text-xs text-slate-500">
            Demo accounts: admin / operator1 / investigator1 / auditor1 — password: sentinel123
          </div>
        </form>
      </div>
    </div>
  );
}

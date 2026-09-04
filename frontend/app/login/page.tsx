"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { LoaderCircle, ShieldCheck } from "lucide-react";
import { api, setToken, setStoredUser, ApiError } from "@/lib/api";
import BrandLogo from "@/components/BrandLogo";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [department, setDepartment] = useState("HQ");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // A brief, one-time transition on success rather than an instant jump —
  // the router.push itself still fires immediately; this only covers the
  // form's own visual state in the moment before the route changes.
  const [success, setSuccess] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.post<any>("/api/auth/login", { username, password, department });
      setToken(res.access_token);
      setStoredUser(res.user);
      setSuccess(true);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-ink px-4">
      <div className="w-full max-w-sm">
        {/* Smart Shield branding — logo falls back to the project's existing
            shield mark (app/icon.svg) until the real file is placed at
            public/branding/smart-shield-logo.png (see that folder's README).
            object-contain: never stretched, aspect ratio always preserved. */}
        <div className="text-center mb-8 animate-scale-in">
          <BrandLogo size={64} className="mx-auto" />
          <div className="text-2xl font-bold tracking-wide mt-4">SENTINEL VISION</div>
          <div className="text-xs text-slate-400 mt-1.5">Unified CCTV Intelligence &amp; Real-Time Smart Policing</div>
          <div className="inline-flex items-center gap-1.5 mt-3 text-[10px] uppercase tracking-wide text-brand-orange/90 border border-brand-orange/30 bg-brand-orange/5 rounded-full px-2.5 py-1">
            <ShieldCheck size={12} strokeWidth={2.25} />
            Smart Shield · Gujarat Police Innovation Challenge 2026
          </div>
        </div>
        <form
          onSubmit={onSubmit}
          className="bg-panel border border-border rounded-lg p-6 space-y-4 animate-slide-up"
          style={{ animationDelay: "80ms" }}
        >
          <div>
            <label className="text-xs text-slate-400">Police ID / Username</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent transition-colors duration-150"
              required
            />
          </div>
          <div>
            <label className="text-xs text-slate-400">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent transition-colors duration-150"
              required
            />
          </div>
          <div>
            <label className="text-xs text-slate-400">Department</label>
            <select
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent transition-colors duration-150"
            >
              <option>HQ</option>
              <option>Ahmedabad</option>
              <option>Surat</option>
              <option>Vadodara</option>
              <option>Rajkot</option>
            </select>
          </div>
          {error && (
            <div className="text-xs text-critical bg-red-500/10 border border-red-500/30 rounded px-3 py-2 animate-slide-up">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 bg-accent text-ink font-medium rounded-md py-2 text-sm transition-all duration-150 hover:opacity-90 active:scale-[0.98] disabled:opacity-60 disabled:active:scale-100"
          >
            {loading && <LoaderCircle size={15} strokeWidth={2.5} className="animate-spin" />}
            {success ? "SIGNED IN" : loading ? "SIGNING IN..." : "LOGIN"}
          </button>
          <div className="text-center text-xs text-slate-500">
            Demo accounts: admin / operator1 / investigator1 / auditor1 — password: sentinel123
          </div>
        </form>
      </div>
    </div>
  );
}

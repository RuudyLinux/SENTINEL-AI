"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { LoaderCircle, ShieldCheck, PlugZap } from "lucide-react";
import { API_BASE, api, setToken, setStoredUser, ApiError, checkBackendIdentity, type ApiPreflight } from "@/lib/api";
import BrandLogo from "@/components/BrandLogo";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [department, setDepartment] = useState("HQ");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // short transition on success; router.push still fires right away, this
  // only covers the form in the moment before the route changes
  const [success, setSuccess] = useState(false);
  // Which backend this dashboard really talks to, checked here since login is
  // the first request. If NEXT_PUBLIC_API_BASE hits some other app (port 8000
  // is contested on dev machines) all you'd see is "Login failed", no way to
  // tell a wrong password from a wrong server.
  const [preflight, setPreflight] = useState<ApiPreflight | null>(null);

  useEffect(() => {
    let cancelled = false;
    checkBackendIdentity().then((result) => {
      if (!cancelled) setPreflight(result);
    });
    return () => {
      cancelled = true;
    };
  }, []);

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
        {/* shield mark (app/icon.svg) unless NEXT_PUBLIC_BRAND_LOGO_URL points
            at a real logo (public/branding/README.md). object-contain, never
            stretched. */}
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
          {/* htmlFor/id so a screen reader doesn't hear two unlabelled boxes;
              autoComplete so a password manager can fill them at shift start */}
          <div>
            <label htmlFor="login-username" className="text-xs text-slate-400">
              Police ID / Username
            </label>
            <input
              id="login-username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent transition-colors duration-150"
              required
            />
          </div>
          <div>
            <label htmlFor="login-password" className="text-xs text-slate-400">
              Password
            </label>
            <input
              id="login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full bg-panel2 border border-border rounded-md px-3 py-2 text-sm outline-none focus:border-accent transition-colors duration-150"
              required
            />
          </div>
          <div>
            <label htmlFor="login-department" className="text-xs text-slate-400">Department</label>
            <select
              id="login-department"
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
          {/* above the credential error on purpose: with a wrong API base
              "Login failed" is true but useless. Login isn't disabled though,
              the check itself can be wrong (proxy, cold start). */}
          {preflight && preflight.status !== "ok" && (
            <div className="text-xs text-medium bg-medium/10 border border-medium/30 rounded px-3 py-2 animate-slide-up flex gap-2">
              <PlugZap size={14} strokeWidth={2.25} className="shrink-0 mt-px" />
              <span>{preflight.message}</span>
            </div>
          )}
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
          {/* the port this build was compiled with. NEXT_PUBLIC_* is inlined
              at build time, so a wrong one needs a rebuild, not a restart */}
          <div className="text-center text-[10px] text-slate-600 break-all">API: {API_BASE}</div>
        </form>
      </div>
    </div>
  );
}

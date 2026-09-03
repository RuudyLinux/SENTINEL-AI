"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, API_BASE } from "@/lib/api";

export default function AddCameraPage() {
  const router = useRouter();
  const [form, setForm] = useState({
    camera_code: "", name: "", department: "Police", location: "", lat: 23.03, lng: 72.58,
    source_type: "video_file", source_uri: "",
    ai_person: true, ai_vehicle: true, ai_anpr: true,
  });
  const [file, setFile] = useState<File | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function uploadIfNeeded(): Promise<string> {
    if (form.source_type !== "video_file" || !file) return form.source_uri;
    const fd = new FormData();
    fd.append("file", file);
    const res = await api.post<any>("/api/cameras/upload-video", fd);
    return res.path;
  }

  async function testConnection() {
    setError(null);
    setTestResult("Testing...");
    try {
      const uri = await uploadIfNeeded();
      const fd = new FormData();
      fd.append("source_type", form.source_type);
      fd.append("source_uri", uri);
      const res = await fetch(`${API_BASE}/api/cameras/test-connection`, { method: "POST", body: fd });
      if (!res.ok) throw new Error(`Test request failed (HTTP ${res.status})`);
      const data = await res.json();
      setTestResult(data.detail);
      if (uri !== form.source_uri) set("source_uri", uri);
    } catch (err: any) {
      setTestResult(null);
      setError(err?.message || "Could not reach the backend to test this source");
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const uri = await uploadIfNeeded();
      const camera = await api.post<any>("/api/cameras", { ...form, source_uri: uri });
      router.push(`/live/${camera.id}`);
    } catch (err: any) {
      setError(err.message || "Failed to save camera");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-lg font-semibold">Add Camera</h1>
      <form onSubmit={save} className="bg-panel border border-border rounded-lg p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Camera Name">
            <input required value={form.name} onChange={(e) => set("name", e.target.value)} className="input" />
          </Field>
          <Field label="Camera ID">
            <input required placeholder="C-014" value={form.camera_code} onChange={(e) => set("camera_code", e.target.value)} className="input" />
          </Field>
          <Field label="Department">
            <input value={form.department} onChange={(e) => set("department", e.target.value)} className="input" />
          </Field>
          <Field label="Location">
            <input value={form.location} onChange={(e) => set("location", e.target.value)} className="input" />
          </Field>
          <Field label="Latitude">
            <input type="number" step="0.0001" value={form.lat} onChange={(e) => set("lat", parseFloat(e.target.value))} className="input" />
          </Field>
          <Field label="Longitude">
            <input type="number" step="0.0001" value={form.lng} onChange={(e) => set("lng", parseFloat(e.target.value))} className="input" />
          </Field>
        </div>

        <Field label="Source Type">
          <select value={form.source_type} onChange={(e) => set("source_type", e.target.value)} className="input">
            <option value="video_file">Uploaded video file (simulated feed)</option>
            <option value="webcam">Webcam (device index)</option>
            <option value="rtsp">RTSP (not supported in this build)</option>
          </select>
        </Field>

        {form.source_type === "video_file" && (
          <Field label="Video File">
            <input type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] || null)} className="text-sm" />
          </Field>
        )}
        {form.source_type === "webcam" && (
          <Field label="Device Index">
            <input value={form.source_uri} onChange={(e) => set("source_uri", e.target.value)} placeholder="0" className="input" />
          </Field>
        )}
        {form.source_type === "rtsp" && (
          <div className="text-xs text-slate-400 bg-panel2 border border-border rounded p-3">
            No real CCTV/RTSP source is available in this environment. The adapter interface supports adding one later
            without changing the detection pipeline — see backend/app/pipeline/source.py.
          </div>
        )}

        <div className="flex gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={form.ai_person} onChange={(e) => set("ai_person", e.target.checked)} /> Person detection</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={form.ai_vehicle} onChange={(e) => set("ai_vehicle", e.target.checked)} /> Vehicle detection</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={form.ai_anpr} onChange={(e) => set("ai_anpr", e.target.checked)} /> ANPR</label>
        </div>

        {testResult && <div className="text-xs text-slate-400">{testResult}</div>}
        {error && <div className="text-xs text-critical">{error}</div>}

        <div className="flex gap-2 pt-2">
          <button type="button" onClick={testConnection} className="text-xs border border-border rounded px-4 py-2 hover:border-accent">TEST CONNECTION</button>
          <button type="submit" disabled={busy} className="text-xs bg-accent text-ink font-medium rounded px-4 py-2 disabled:opacity-50">
            {busy ? "Saving..." : "SAVE CAMERA"}
          </button>
          <button type="button" onClick={() => router.push("/cameras")} className="text-xs border border-border rounded px-4 py-2">CANCEL</button>
        </div>
      </form>
      <style jsx global>{`
        .input { width: 100%; background: #161f2c; border: 1px solid #22303f; border-radius: 6px; padding: 8px 10px; font-size: 13px; margin-top: 4px; }
      `}</style>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-slate-400">
      {label}
      {children}
    </label>
  );
}

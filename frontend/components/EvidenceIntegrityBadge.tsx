import { ShieldCheck, ShieldAlert, ShieldQuestion, ShieldX, type LucideIcon } from "lucide-react";

/** 10/10 roadmap P8: the exact wording an operator sees must never overstate
 * what was actually checked. Four honest states only — never a generic
 * "OK"/"checked" that would blur VERIFIED (matches its capture-time digest)
 * with NOT YET VERIFIED (no check has run) or UNVERIFIABLE (the file/digest
 * itself is gone) or NO BASELINE (predates capture-time hashing, so a match
 * now proves nothing about what was originally captured). Backed by
 * app/routers/evidence.py::verify_evidence — see its docstring for the same
 * four outcomes on the API side. */
const STATUS_MAP: Record<string, { label: string; cls: string; Icon: LucideIcon }> = {
  verified: { label: "VERIFIED", cls: "bg-green-500/15 text-green-400 border border-green-500/30", Icon: ShieldCheck },
  tampered: { label: "TAMPERED", cls: "bg-red-500/15 text-red-400 border border-red-500/30", Icon: ShieldX },
  unverifiable: { label: "UNVERIFIABLE", cls: "bg-orange-500/15 text-orange-400 border border-orange-500/30", Icon: ShieldAlert },
  no_baseline: { label: "NO CAPTURE-TIME BASELINE", cls: "bg-yellow-500/15 text-yellow-400 border border-yellow-500/30", Icon: ShieldQuestion },
  unverified: { label: "NOT YET VERIFIED", cls: "bg-slate-500/15 text-slate-400 border border-slate-500/30", Icon: ShieldQuestion },
};

export default function EvidenceIntegrityBadge({ status }: { status: string | null | undefined }) {
  const entry = STATUS_MAP[(status || "unverified").toLowerCase()] || STATUS_MAP.unverified;
  const { label, cls, Icon } = entry;
  return (
    <span className={`badge ${cls}`} title="Evidence integrity — see app/routers/evidence.py::verify_evidence">
      <Icon size={11} strokeWidth={2.5} aria-hidden="true" />
      {label}
    </span>
  );
}

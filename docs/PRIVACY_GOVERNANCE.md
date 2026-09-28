# Privacy & governance controls

**This document is not legal advice, and SENTINEL VISION does not certify
compliance with any specific law or policy.** CCTV/ANPR surveillance is
regulated differently by jurisdiction and by agency policy; this platform
provides *mechanism* — configurable retention, an audited deletion workflow,
access control, and purpose logging — that a deploying agency configures and
operates according to whatever law/policy actually applies to it. Nothing
below should be read as a claim that using these controls makes a deployment
lawful.

## What exists today

| Control | Where | Notes |
|---|---|---|
| Role-based access control | `app/security.py` (`require_roles`), five roles seeded in `app/seed.py` | Administrator / Control Room Operator / Investigator / Supervisor / Auditor — least-privilege by role, not a single shared login. |
| Full access audit trail | `app/audit.py`, `app/routers/audit.py` | Every sensitive action (evidence download, watchlist change, camera access, incident actions, governance purges) is logged with actor, action, resource, result, timestamp — tamper-evident via a hash chain (see `docs/THREAT_MODEL.md` → evidence tampering / audit tampering). |
| Configurable evidence retention | `settings.evidence_retention_days` (`app/config.py`) | `None` by default — no automatic expiry until an administrator explicitly sets a period for their deployment. Never a hardcoded number, because no single number is correct across jurisdictions. |
| Audited, confirmed deletion workflow | `POST /api/governance/purge-expired` (`app/routers/governance.py`) | Administrator-only. Dry-run by default; a real deletion requires BOTH `dry_run: false` AND `confirm: true` in the same request. Every real purge is logged with exactly which evidence ids were removed and by whom. |
| Watchlist removal actually removes | `app/watchlist.py`, `app/routers/watchlists.py` | Deactivating an entry stops the alerts, clears the vehicle's cached watchlist flag (which also stops snapshot evidence being captured of it), and drops it from the in-force listing. **This did not hold before 2026-09-12**: the flag was never recomputed, so a plate removed from the watchlist kept producing CRITICAL alerts and kept being photographed indefinitely. Covered by `tests/test_watchlist_lifecycle.py`. |
| Time-limited watchlisting | `WatchlistEntry.valid_until`, honoured by `app/watchlist.py` | An entry can be given an end date, after which it no longer matches. **Also new on 2026-09-12**: the column existed, was accepted by the API and stored, and was never compared to the clock anywhere in the codebase — so an entry with an expiry matched forever. An operator setting an end date is making a proportionality decision, and the system must keep it. |
| Watchlist history is retained, not deleted | `GET /api/watchlists?include_inactive=true` | Deactivation is a soft delete: the row stays for audit (who watchlisted a plate, when, and why), while the default listing shows only what is in force. Removing someone from surveillance and erasing the record of having surveilled them are different things, and only the first is what deactivation means here. |

| Evidence export restriction | `app/routers/evidence.py` (`/file-token`, `/package-token`) | Evidence files and packages are served only via short-lived, resource-scoped signed tokens obtained through an RBAC-checked, audited endpoint — never a bare, permanently-valid URL. |
| Plate redaction in exports | `GET /api/evidence/incidents/{id}/package?redact=true` (`app/routers/evidence.py::_redact_package`) | Opt-in. Masks the registration EVERYWHERE it appears — the plate reaches that document through five independent paths (`vehicle.plate_text`, `alert.reasons`, `incident.title`, `incident.description`, `audit_trail[].resource`), so a field-by-field mask would look redacted while still disclosing it. Evidence ids, SHA-256 digests and verification statuses are never masked, so a redacted package remains verifiable. Which mode was exported is recorded in the audit trail and inside the package. |
| Purpose/reason logging | `Alert.feedback_reason`, `Plate` review reason, `WatchlistEntry.reason`, incident notes | Operator actions that affect intelligence outcomes carry a stated reason, not just an outcome flag — an auditor reviewing the trail can see *why*, not only *what*. |
| Operator accountability | `feedback_by`, `reviewed_by`, `acknowledged_by`, `assigned_to`, `AuditLog.username` | Every consequential action is attributable to a named operator, never anonymous. |

## Explicitly NOT provided

- **A legally-mandated retention period.** `evidence_retention_days` is a
  mechanism an administrator configures — this platform does not know, and
  does not claim to know, what period is legally required for a given
  deployment.
- **Image redaction.** Plate TEXT can now be masked in evidence packages
  (see the table above), but the evidence IMAGES themselves are exported
  unaltered — a snapshot still shows the plate. Blurring pixels in an
  evidence image is deliberately not offered: altering the image would
  break its capture-time SHA-256, which is the whole basis of the integrity
  claim. An export that needs redacted imagery should be produced as a
  separate derived artefact, not by modifying evidence.
- **Automated legal-hold enforcement.** An administrator can purge expired
  evidence even if it is relevant to an active investigation; there is no
  automatic hold mechanism tied to `Incident.status`. Operationally, keep an
  incident's evidence out of the retention window by not configuring a
  retention period shorter than active-case timelines, or by extending this
  module before relying on it in a real deployment.
- **Data-subject access/erasure requests** (e.g. GDPR-style individual
  rights) — no self-service mechanism exists for a person to request what
  data the platform holds about their vehicle/plate.

## Operating guidance

1. Decide the applicable retention period(s) with your legal/compliance
   function — this varies by jurisdiction, evidence type, and whether an
   investigation is active — and set `EVIDENCE_RETENTION_DAYS` accordingly.
2. Before running a real (non-dry-run) purge, cross-check
   `GET /api/governance/retention-policy`'s `eligible_for_purge` count and,
   ideally, the actual `eligible_ids` from a dry-run call against any open
   incidents/investigations that must be exempted.
3. Review `GET /api/audit/verify-chain` periodically (see
   `docs/THREAT_MODEL.md`) — an intact chain is part of the evidence that the
   audit trail itself, including governance actions, has not been tampered
   with.

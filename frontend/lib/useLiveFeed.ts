"use client";
import { useCallback, useMemo, useRef, useState } from "react";
import { useLiveSocket, type LiveEvent } from "./useLiveSocket";

/** Canonical backend event names (backend app/ws.py `EventType`). Kept in one
 * place so a filter is never a magic string scattered across components. */
export const EVENT = {
  DETECTION_CREATED: "detection.created",
  DETECTION_BATCH: "detection.batch",
  VEHICLE_SIGHTING: "vehicle.sighting",
  ALERT_CREATED: "alert.created",
  INCIDENT_CREATED: "incident.created",
  CAMERA_STATUS: "camera.status",
  CAMERA_HEALTH: "camera.health",
  SELF_HEAL_RECOVERY: "self_heal.recovery",
} as const;

export type FeedKind = "detection" | "sighting" | "alert" | "incident" | "system";

export type FeedItem = {
  /** Stable key for React. The backend does not guarantee a unique id on every
   * event type, so this is composed locally and never used as a domain id. */
  key: string;
  kind: FeedKind;
  type: string;
  at: number;
  data: any;
};

/** How the event vocabulary maps onto the operator-facing groupings the control
 * room filters by. */
const KIND_BY_TYPE: Record<string, FeedKind> = {
  [EVENT.DETECTION_CREATED]: "detection",
  [EVENT.VEHICLE_SIGHTING]: "sighting",
  [EVENT.ALERT_CREATED]: "alert",
  [EVENT.INCIDENT_CREATED]: "incident",
  [EVENT.SELF_HEAL_RECOVERY]: "system",
  [EVENT.CAMERA_STATUS]: "system",
  [EVENT.CAMERA_HEALTH]: "system",
};

/** Hard cap on retained events.
 *
 * A control room is left open for a whole shift against a live detection
 * stream, so an unbounded list is a guaranteed browser slowdown. Older items
 * fall off the end; the durable record is the database, which the rest of the
 * app queries — this list is a live view, never a store. */
const DEFAULT_LIMIT = 250;

type Options = {
  limit?: number;
  /** Only retain these kinds. Undefined keeps everything. */
  kinds?: FeedKind[];
};

export function useLiveFeed(options: Options = {}) {
  const limit = options.limit ?? DEFAULT_LIMIT;
  const [items, setItems] = useState<FeedItem[]>([]);
  const [counts, setCounts] = useState<Record<FeedKind, number>>({
    detection: 0, sighting: 0, alert: 0, incident: 0, system: 0,
  });
  const [paused, setPaused] = useState(false);
  // Read inside the socket callback, which is registered once — a state value
  // would be captured stale there, so the live value lives in a ref.
  const pausedRef = useRef(false);
  const seq = useRef(0);
  const kinds = options.kinds;

  const setPausedBoth = useCallback((value: boolean) => {
    pausedRef.current = value;
    setPaused(value);
  }, []);

  const handle = useCallback(
    (event: LiveEvent) => {
      // Detections arrive coalesced (backend ws.py batches them so N cameras do
      // not produce N x inference-rate React updates per second). Unpacking
      // here means every consumer sees plain events and never has to know that
      // batching exists.
      const incoming: { type: string; data: any }[] =
        event.type === EVENT.DETECTION_BATCH
          ? (event.data?.events ?? []).map((e: any) => ({ type: e.type ?? EVENT.DETECTION_CREATED, data: e }))
          : [{ type: event.type, data: event.data }];

      const mapped: FeedItem[] = [];
      const delta: Partial<Record<FeedKind, number>> = {};
      for (const one of incoming) {
        const kind = KIND_BY_TYPE[one.type];
        if (!kind) continue; // bulk_progress and friends are not feed events
        delta[kind] = (delta[kind] ?? 0) + 1;
        if (kinds && !kinds.includes(kind)) continue;
        mapped.push({ key: `${one.type}-${seq.current++}`, kind, type: one.type, at: Date.now(), data: one.data });
      }
      if (mapped.length === 0 && Object.keys(delta).length === 0) return;

      // Counters keep advancing while paused — the operator paused the SCROLL,
      // not the system, and a frozen throughput number would misrepresent it.
      setCounts((prev) => {
        const next = { ...prev };
        for (const [kind, n] of Object.entries(delta)) next[kind as FeedKind] += n as number;
        return next;
      });
      if (pausedRef.current || mapped.length === 0) return;
      setItems((prev) => [...mapped.reverse(), ...prev].slice(0, limit));
    },
    [kinds, limit],
  );

  const { connected } = useLiveSocket(handle);

  const clear = useCallback(() => setItems([]), []);

  return useMemo(
    () => ({ items, counts, connected, paused, setPaused: setPausedBoth, clear }),
    [items, counts, connected, paused, setPausedBoth, clear],
  );
}

"use client";
import { useCallback, useMemo, useRef, useState } from "react";
import { useLiveSocket, type LiveEvent } from "./useLiveSocket";

/** Backend event names (app/ws.py EventType), in one place instead of magic strings. */
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
  /** React key, built locally since not every event has a unique id. Never a
   * domain id. */
  key: string;
  kind: FeedKind;
  type: string;
  at: number;
  data: any;
};

/** Event types -> the groupings the control room filters by. */
const KIND_BY_TYPE: Record<string, FeedKind> = {
  [EVENT.DETECTION_CREATED]: "detection",
  [EVENT.VEHICLE_SIGHTING]: "sighting",
  [EVENT.ALERT_CREATED]: "alert",
  [EVENT.INCIDENT_CREATED]: "incident",
  [EVENT.SELF_HEAL_RECOVERY]: "system",
  [EVENT.CAMERA_STATUS]: "system",
  [EVENT.CAMERA_HEALTH]: "system",
};

/** Max events kept. A control room stays open all shift on a live stream, so
 * unbounded means a slow browser. Old ones fall off; the DB is the record,
 * this is just a live view. */
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
  // read inside the socket callback (registered once), so it lives in a ref
  // rather than stale state
  const pausedRef = useRef(false);
  const seq = useRef(0);
  const kinds = options.kinds;

  const setPausedBoth = useCallback((value: boolean) => {
    pausedRef.current = value;
    setPaused(value);
  }, []);

  const handle = useCallback(
    (event: LiveEvent) => {
      // detections come batched from ws.py; unpacked here so consumers only
      // see plain events
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

      // counters keep going while paused, the operator paused the scroll, not
      // the system
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

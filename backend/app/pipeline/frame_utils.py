"""Frame drawing and snapshot/row helpers, extracted from worker.py.

Four small, generic functions with no camera-loop state of their own --
each takes exactly the frame/row/values it operates on and returns a
result, nothing implicit. `_draw_boxes` and `_save_snapshot` work on a raw
frame; `_snapshot_attrs`/`_restore_row` are the capture/restore halves of
the "reassign these fields after a rollback expired them" pattern used
throughout the ANPR and correlation retry paths (see worker.py's own
_PLATE_REAPPLY_FIELDS/_TRACK_REAPPLY_FIELDS for why that pattern exists --
those field lists stayed in worker.py, next to the code that actually uses
them, rather than following these generic helpers here).
"""
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from ..config import settings


def _draw_boxes(frame: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = (0, 255, 0) if d["cls"] == "person" else (0, 165, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f'{d["cls"]} {d["confidence"]:.2f}'
        cv2.putText(frame, label, (x1, max(0, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return frame


def _save_snapshot(frame: np.ndarray, prefix: str) -> str:
    fname = f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = settings.evidence_dir / fname
    cv2.imwrite(str(path), frame)
    return str(path)


def _snapshot_attrs(row: Any, fields: "tuple[str, ...]") -> "dict[str, Any] | None":
    """Capture a row's current values for the fields a retry must restore."""
    if row is None:
        return None
    return {name: getattr(row, name) for name in fields}


def _restore_row(db: Session, row: Any, values: "dict[str, Any] | None") -> None:
    """Re-attach a row and reassign the captured values. `db.add` is a no-op for
    a row that is still attached, and re-attaches one a rollback detached."""
    if row is None:
        return
    db.add(row)
    for name, value in (values or {}).items():
        setattr(row, name, value)

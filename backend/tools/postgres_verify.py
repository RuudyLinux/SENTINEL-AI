"""Live PostgreSQL verification — the production datastore, actually exercised.

Everything this repo claims about PostgreSQL was previously verified only by
generating SQL offline (`alembic upgrade head --sql`). That proves the DDL
parses; it does not prove the migration APPLIES, that the constraints behave,
or that code paths which differ between the two backends work. This runs a
REAL PostgreSQL server (via the `pgserver` package — self-contained binaries,
no admin install, no system service) and exercises them for real.

Everything happens in ONE process on purpose: `pgserver` stops the server when
the owning process exits, so a separate `alembic` invocation would find nothing
listening.

Checks, in order:

1. `alembic upgrade head` APPLIES against a real PostgreSQL database.
2. The schema really carries the constraints this project depends on —
   `vehicles.plate_text` UNIQUE (BUG-1) and `audit_logs.chain_seq` UNIQUE.
3. Those constraints actually bite (duplicate insert rejected).
4. Foreign keys are enforced — the property SQLite only gained once
   `PRAGMA foreign_keys=ON` was set, and the source of the dev/prod divergence.
5. **BUG-D**: `seed.reset_demo_data` runs cleanly. This is the important one —
   BUG-D was a PostgreSQL-ONLY failure (a missing `IncidentAlert` delete made
   `DELETE FROM incidents` a ForeignKeyViolation), so it was fixed against a
   backend that could not previously be tested. This proves the fix on the
   backend that was actually broken.
6. `alembic downgrade base` reverses the whole chain cleanly.

Usage:
    cd backend
    uv pip install --python .venv/Scripts/python.exe pgserver   # one-off
    .venv/Scripts/python.exe tools/postgres_verify.py

`pgserver` is deliberately NOT in requirements.txt: it bundles a full
PostgreSQL distribution and is only needed to RUN this verification, never to
run SENTINEL itself. A deployment that already has a PostgreSQL instance can
skip it and point DATABASE_URL at the real server instead.

Measured result on this machine (2026-09-11): 12/12 checks passed, including
pg_dump -> DROP DATABASE -> pg_restore with the evidence SHA-256 intact.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PGDATA = Path(tempfile.gettempdir()) / "pgdata_sentinel_verify"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    import pgserver
    import psycopg

    print(f"starting PostgreSQL (data dir: {PGDATA})")
    server = pgserver.get_server(str(PGDATA))
    admin_uri = server.get_uri()
    db_name = f"sentinel_verify_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")
    raw_uri = admin_uri.rsplit("/", 1)[0] + f"/{db_name}"
    sa_uri = raw_uri.replace("postgresql://", "postgresql+psycopg://")
    print(f"database: {db_name}")

    # Must be set BEFORE app.config is imported — settings are read at import.
    os.environ["DATABASE_URL"] = sa_uri

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    # --- 1. migrations actually apply -------------------------------------
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        command.upgrade(cfg, "head")
        check("alembic upgrade head applies to a real PostgreSQL database", True)
    except Exception as exc:
        check("alembic upgrade head applies to a real PostgreSQL database", False, repr(exc))
        return _summary()

    engine = create_engine(sa_uri)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    check("core tables created", {"cameras", "vehicles", "alerts", "incidents", "evidence", "audit_logs"} <= tables,
          f"{len(tables)} tables")

    # --- 2/3. the constraints this project depends on ---------------------
    def _has_unique(table: str, column: str) -> bool:
        uniques = inspector.get_unique_constraints(table)
        indexes = [i for i in inspector.get_indexes(table) if i.get("unique")]
        return any(column in u["column_names"] for u in uniques) or any(
            column in i["column_names"] for i in indexes
        )

    check("vehicles.plate_text carries a UNIQUE constraint (BUG-1)", _has_unique("vehicles", "plate_text"))
    check("audit_logs.chain_seq carries a UNIQUE constraint", _has_unique("audit_logs", "chain_seq"))

    plate = f"GJ05PG{uuid.uuid4().hex[:4].upper()}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO vehicles (id, plate_text) VALUES (:i, :p)"), {"i": "veh_pg_1", "p": plate})
    duplicate_rejected = False
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO vehicles (id, plate_text) VALUES (:i, :p)"), {"i": "veh_pg_2", "p": plate})
    except Exception:
        duplicate_rejected = True
    check("duplicate plate_text is rejected by PostgreSQL (BUG-1 fix is real here)", duplicate_rejected)

    # --- 4. foreign keys enforced -----------------------------------------
    fk_rejected = False
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO detections (id, camera_id, cls, confidence, bbox) "
                     "VALUES (:i, :c, 'car', 0.9, '[0,0,1,1]')"),
                {"i": "det_pg_1", "c": "cam_does_not_exist"},
            )
    except Exception:
        fk_rejected = True
    check("foreign keys are enforced (detection -> nonexistent camera rejected)", fk_rejected)

    # --- 4b. analytics queries run on PostgreSQL, not only on SQLite ------
    # `/api/analytics/events-by-hour` grouped with SQLite's `strftime`, which
    # SQLAlchemy passes through verbatim, so on PostgreSQL it failed with
    # "function strftime(unknown, timestamp without time zone) does not
    # exist" — a 500 on the dashboard's 24-hour chart in production, invisible
    # to a test suite that runs on SQLite. The real endpoint function is
    # called here (not a re-written copy of its query) so this check cannot
    # drift away from what the API actually executes.
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from app.routers.analytics import events_by_hour

    _Session = _sessionmaker(bind=engine)
    _session = _Session()
    try:
        events_by_hour(db=_session, user=None)
        check("analytics events-by-hour executes on PostgreSQL (no SQLite-only SQL)", True)
    except Exception as exc:
        check("analytics events-by-hour executes on PostgreSQL (no SQLite-only SQL)", False, repr(exc))
    finally:
        _session.close()

    # --- 5. BUG-D: the PostgreSQL-only demo-reset failure ------------------
    from sqlalchemy.orm import sessionmaker

    from app import models
    from app.seed import reset_demo_data

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        camera = models.Camera(
            camera_code=f"PG-{uuid.uuid4().hex[:8]}", name="pg probe",
            source_type="video_file", source_uri="x.mp4",
        )
        session.add(camera)
        session.flush()
        detection = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[0, 0, 1, 1])
        session.add(detection)
        session.flush()
        alert = models.Alert(camera_id=camera.id, severity="CRITICAL", detection_id=detection.id, reasons=["pg probe"])
        session.add(alert)
        session.flush()
        incident = models.Incident(title="pg probe incident", camera_id=camera.id, alert_id=alert.id)
        session.add(incident)
        session.flush()
        # The row whose missing DELETE was BUG-D.
        session.add(models.IncidentAlert(incident_id=incident.id, alert_id=alert.id, correlation_reason="pg probe"))
        session.add(models.Evidence(
            incident_id=incident.id, camera_id=camera.id, alert_id=alert.id,
            detection_id=detection.id, evidence_type="snapshot", sha256="ab" * 32,
        ))
        session.commit()

        reset_demo_data(session)
        remaining = (
            session.query(models.IncidentAlert).count()
            + session.query(models.Incident).count()
            + session.query(models.Alert).count()
        )
        check("BUG-D: reset_demo_data succeeds on PostgreSQL and clears links", remaining == 0,
              f"{remaining} rows left")
    except Exception as exc:
        check("BUG-D: reset_demo_data succeeds on PostgreSQL and clears links", False, repr(exc))
        session.rollback()
    finally:
        session.close()

    # --- 5b. disaster recovery on PostgreSQL ------------------------------
    # tests/test_disaster_recovery.py proves backup/restore for SQLite only
    # (it uses SQLite's own online-backup API). This is the PostgreSQL
    # equivalent, using the real pg_dump/pg_restore that ship with the server
    # — the documented production DR path, previously never exercised.
    import subprocess

    pg_bin = Path(pgserver.__file__).parent / "pginstall" / "bin"
    dump_path = Path(tempfile.gettempdir()) / f"sentinel_dr_{uuid.uuid4().hex[:8]}.dump"
    session = Session()
    try:
        camera = models.Camera(
            camera_code=f"DR-{uuid.uuid4().hex[:8]}", name="pg dr probe",
            source_type="video_file", source_uri="x.mp4",
        )
        session.add(camera)
        session.flush()
        incident = models.Incident(title="pg dr incident", camera_id=camera.id, status="open")
        session.add(incident)
        session.flush()
        digest = "cd" * 32
        session.add(models.Evidence(
            incident_id=incident.id, camera_id=camera.id, evidence_type="snapshot",
            sha256=digest, verification_status="verified",
        ))
        session.commit()
        incident_id, camera_id = incident.id, camera.id
    finally:
        session.close()

    dumped = subprocess.run(
        [str(pg_bin / "pg_dump"), "-Fc", "-d", raw_uri, "-f", str(dump_path)],
        capture_output=True, text=True,
    )
    if dumped.returncode != 0:
        check("PostgreSQL DR: pg_dump succeeds", False, dumped.stderr.strip()[:200])
    else:
        check("PostgreSQL DR: pg_dump succeeds", True, f"{dump_path.stat().st_size} bytes")

        # The disaster: drop the whole database.
        engine.dispose()
        with psycopg.connect(admin_uri, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {db_name} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {db_name}")

        restored = subprocess.run(
            [str(pg_bin / "pg_restore"), "-d", raw_uri, str(dump_path)],
            capture_output=True, text=True,
        )
        check("PostgreSQL DR: pg_restore succeeds after a full database drop",
              restored.returncode == 0, restored.stderr.strip()[:200])

        verify_engine = create_engine(sa_uri)
        VerifySession = sessionmaker(bind=verify_engine)
        vs = VerifySession()
        try:
            inc = vs.query(models.Incident).filter(models.Incident.id == incident_id).first()
            ev = vs.query(models.Evidence).filter(models.Evidence.camera_id == camera_id).first()
            check("PostgreSQL DR: incident survived the restore", inc is not None and inc.status == "open")
            check("PostgreSQL DR: evidence digest survived the restore byte-for-byte",
                  ev is not None and ev.sha256 == digest)
        finally:
            vs.close()
            verify_engine.dispose()
        dump_path.unlink(missing_ok=True)
        engine = create_engine(sa_uri)

    # --- 6. the migration chain reverses ----------------------------------
    try:
        command.downgrade(cfg, "base")
        remaining_tables = set(inspect(create_engine(sa_uri)).get_table_names()) - {"alembic_version"}
        check("alembic downgrade base reverses the whole chain", not remaining_tables,
              f"left: {sorted(remaining_tables)}" if remaining_tables else "")
    except Exception as exc:
        check("alembic downgrade base reverses the whole chain", False, repr(exc))

    engine.dispose()
    return _summary()


def _summary() -> int:
    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

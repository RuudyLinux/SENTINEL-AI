"""Live PostGIS verification against a running PostgreSQL + PostGIS server.

The test suite runs on SQLite, where app/geo.py uses its haversine fallback.
This proves the PostGIS path on a real server:

1. `alembic upgrade head` applies, including the PostGIS migration.
2. `alembic check` finds no drift (the PostGIS-owned objects are filtered).
3. `cameras.geog` exists, is generated from lat/lng, is NULL for 0,0, and has
   a GiST index.
4. The indexed ST_DWithin search returns the same cameras, in the same order,
   as the haversine fallback, with distances within 0.5% (spheroid vs sphere).
5. `alembic downgrade base` then `upgrade head` re-applies cleanly.

Usage (a throwaway server, e.g. the compose image):
    docker run -d --name sentinel-postgis -e POSTGRES_PASSWORD=verify -p 55432:5432 postgis/postgis:16-3.5-alpine
    cd backend
    DATABASE_URL=postgresql+psycopg://postgres:verify@127.0.0.1:55432/postgres python tools/postgis_verify.py
It creates and drops its own tables; point it only at a scratch database.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def alembic(*args: str) -> None:
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        raise SystemExit(f"alembic {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
    print(f"ok   alembic {' '.join(args)}")


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        raise SystemExit("set DATABASE_URL to a scratch PostgreSQL + PostGIS database")
    alembic("upgrade", "head")
    alembic("check")

    from sqlalchemy import text
    from app import geo, models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        col = db.execute(text(
            "SELECT is_generated, udt_name FROM information_schema.columns "
            "WHERE table_name = 'cameras' AND column_name = 'geog'")).first()
        assert col is not None and col.is_generated == "ALWAYS" and col.udt_name == "geography", col
        idx = db.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_cameras_geog'")).scalar()
        assert idx and "gist" in idx.lower(), idx
        print("ok   cameras.geog is a generated geography column with a GiST index")

        points = {"PG-PALDI": (23.0120, 72.5627), "PG-NEAR": (23.0160, 72.5627), "PG-MID": (23.0250, 72.5700),
                  "PG-FAR": (23.0400, 72.5627), "PG-UNKNOWN": (0.0, 0.0)}
        db.query(models.Camera).filter(models.Camera.camera_code.in_(points)).delete(synchronize_session=False)
        for code, (lat, lng) in points.items():
            db.add(models.Camera(camera_code=code, name=code, source_type="mock_vms", source_uri="", lat=lat, lng=lng))
        db.commit()
        unknown = db.execute(text("SELECT geog IS NULL FROM cameras WHERE camera_code = 'PG-UNKNOWN'")).scalar()
        assert unknown is True, "0,0 must generate a NULL geography"
        print("ok   an unknown (0,0) position generates NULL, not a point in the Gulf of Guinea")

        assert geo.postgis_ready(db) is True
        lat, lng = points["PG-PALDI"]
        for radius in (500, 2000, 5000):
            spatial = geo.nearby_cameras(db, lat, lng, radius, 50)
            geo._postgis = False  # force the fallback for comparison
            fallback = geo.nearby_cameras(db, lat, lng, radius, 50)
            geo._postgis = True
            codes_s = [c.camera_code for c, _ in spatial if c.camera_code in points]
            codes_f = [c.camera_code for c, _ in fallback if c.camera_code in points]
            assert codes_s == codes_f, (radius, codes_s, codes_f)
            for (_, ds), (_, df) in zip(spatial, fallback):
                assert abs(ds - df) <= max(1.0, 0.005 * df), (ds, df)
            print(f"ok   radius {radius} m: PostGIS {codes_s} "
                  f"[{', '.join(f'{d:.1f}' for c, d in spatial if c.camera_code in points)} m] matches the fallback")
        plan = "\n".join(r[0] for r in db.execute(text(
            "EXPLAIN SELECT id FROM cameras WHERE ST_DWithin(geog, "
            "ST_SetSRID(ST_MakePoint(72.56, 23.01), 4326)::geography, 1000)")))
        print("info query plan:", " | ".join(line.strip() for line in plan.splitlines()[:3]))
        db.query(models.Camera).filter(models.Camera.camera_code.in_(points)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    alembic("downgrade", "base")
    alembic("upgrade", "head")
    alembic("check")
    print("PASS PostGIS path verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

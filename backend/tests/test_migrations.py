"""The Alembic chain must apply, match the models, and roll back — on SQLite too.

`alembic upgrade head` ABORTED on SQLite:

    NotImplementedError: No support for ALTER of constraints in SQLite dialect.

Two post-baseline migrations used bare `op.create_foreign_key` /
`op.create_unique_constraint`, which SQLite cannot do (the baseline migration
had correctly used `batch_alter_table` throughout). So the migration chain
could not be applied to SQLite at all, and CI's own "Verify migrations apply
and roll back" step could never have passed — nobody noticed because those
jobs only run on pushes to `main`, and day-to-day SQLite development uses the
additive helpers in `app/db.py`, never Alembic.

Fixing that exposed a second defect hiding behind the first: `alembic check`
then reported permanent drift, because the migrations created a unique
CONSTRAINT plus a separate non-unique index while `models.py` declares
`Column(..., unique=True, index=True)` — which SQLAlchemy renders as exactly
ONE unique index.

Run in a subprocess with its own DB_PATH, mirroring how CI invokes Alembic:
the settings object is already loaded by the time this test runs, so an
in-process override would not reach the migration environment.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic(command: str, db_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        # Keep the migration environment away from the real camera grid, the
        # same way the CI job does.
        "SENTINEL_GRID_EMAIL": "",
        "SENTINEL_GRID_PASSWORD": "",
        "SENTINEL_GRID_AUTOCONNECT": "false",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_BACKEND_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture
def scratch_db(tmp_path) -> Path:
    return tmp_path / "migration-check.db"


class TestMigrationChain:
    def test_upgrade_head_applies_to_sqlite(self, scratch_db):
        result = _alembic("upgrade head", scratch_db)
        assert result.returncode == 0, (
            "alembic upgrade head failed on SQLite:\n" + result.stderr[-2000:]
        )
        assert scratch_db.exists()

    def test_the_migrated_schema_matches_the_models(self, scratch_db):
        """`alembic check` on a database freshly migrated to head must find
        nothing to do. Drift here means the models and the migrations disagree
        — the failure mode where a column exists in one environment and not
        another."""
        assert _alembic("upgrade head", scratch_db).returncode == 0
        result = _alembic("check", scratch_db)
        assert result.returncode == 0, (
            "model/migration drift after upgrading to head:\n" + (result.stderr or result.stdout)[-2000:]
        )

    def test_downgrade_base_reverses_the_whole_chain(self, scratch_db):
        assert _alembic("upgrade head", scratch_db).returncode == 0
        result = _alembic("downgrade base", scratch_db)
        assert result.returncode == 0, (
            "alembic downgrade base failed on SQLite:\n" + result.stderr[-2000:]
        )

    def test_the_chain_is_re_appliable(self, scratch_db):
        """upgrade -> downgrade -> upgrade. A migration whose downgrade leaves
        a stray index or constraint behind passes the first two steps and fails
        the third."""
        assert _alembic("upgrade head", scratch_db).returncode == 0
        assert _alembic("downgrade base", scratch_db).returncode == 0
        result = _alembic("upgrade head", scratch_db)
        assert result.returncode == 0, (
            "the chain could not be re-applied after a full downgrade:\n" + result.stderr[-2000:]
        )


class TestUniquenessSurvivesTheChain:
    def test_plate_text_is_unique_after_migrating(self, scratch_db):
        """BUG-1's database-level guard must actually exist in a migrated
        database, not only in one built by `Base.metadata.create_all`."""
        import sqlite3

        assert _alembic("upgrade head", scratch_db).returncode == 0
        conn = sqlite3.connect(scratch_db)
        try:
            indexes = conn.execute("PRAGMA index_list('vehicles')").fetchall()
            unique_cols = set()
            for row in indexes:
                name, is_unique = row[1], row[2]
                if is_unique:
                    unique_cols.update(c[2] for c in conn.execute(f"PRAGMA index_info('{name}')"))
            assert "plate_text" in unique_cols, f"no unique index on vehicles.plate_text: {indexes}"

            conn.execute("INSERT INTO vehicles (id, plate_text) VALUES ('v1', 'GJ05MG1234')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO vehicles (id, plate_text) VALUES ('v2', 'GJ05MG1234')")
        finally:
            conn.close()

    def test_chain_seq_is_unique_after_migrating(self, scratch_db):
        import sqlite3

        assert _alembic("upgrade head", scratch_db).returncode == 0
        conn = sqlite3.connect(scratch_db)
        try:
            unique_cols = set()
            for row in conn.execute("PRAGMA index_list('audit_logs')").fetchall():
                if row[2]:
                    unique_cols.update(c[2] for c in conn.execute(f"PRAGMA index_info('{row[1]}')"))
            assert "chain_seq" in unique_cols
        finally:
            conn.close()

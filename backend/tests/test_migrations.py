"""The Alembic chain applies, matches the models and rolls back, on SQLite too.

`alembic upgrade head` used to abort on SQLite:

    NotImplementedError: No support for ALTER of constraints in SQLite dialect.

Two migrations used bare op.create_foreign_key / op.create_unique_constraint
(the baseline used batch mode). CI's migration step only runs on main and
dev uses db.py's additive helpers, so nobody noticed.

Fixing that showed `alembic check` drifting forever: the migrations made a
unique constraint plus a plain index, while unique=True, index=True renders
to one unique index.

Runs in a subprocess with its own DB_PATH like CI does; settings are already
loaded in this process, so an in-process override wouldn't reach Alembic.
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
        """Freshly migrated to head, `alembic check` finds nothing. Drift means
        models and migrations disagree."""
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
        """upgrade, downgrade, upgrade. A downgrade leaving an index behind
        passes the first two and fails the third."""
        assert _alembic("upgrade head", scratch_db).returncode == 0
        assert _alembic("downgrade base", scratch_db).returncode == 0
        result = _alembic("upgrade head", scratch_db)
        assert result.returncode == 0, (
            "the chain could not be re-applied after a full downgrade:\n" + result.stderr[-2000:]
        )


class TestUniquenessSurvivesTheChain:
    def test_plate_text_is_unique_after_migrating(self, scratch_db):
        """The plate_text unique index exists in a migrated DB too, not only
        one from create_all."""
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

"""The compose/Dockerfile contract, checked against the application itself.

**This is not a substitute for `docker compose up --build`.** A real build is
the only thing that proves the images build and the services come up, and it
cannot run in this environment (Docker Desktop's installer requires an
elevation prompt nobody can click from here). That limitation is stated in the
README rather than papered over.

What CAN be checked without Docker is the contract between those files and the
code — which is where silent drift actually lives, because nothing fails
loudly when it breaks:

* A volume is mounted at a path the app no longer writes to. Evidence would be
  written INSIDE the container's writable layer and vanish on the next
  `docker compose up --build` — for a platform whose entire premise is
  chain-of-custody, that is the worst possible way to lose data, and both a
  unit test suite and a green `docker ps` would look perfectly healthy.
* An environment variable is set in compose that the settings object does not
  read (a rename, a typo). The service starts, ignores it, and runs on the
  default — so a deployment sets `METRICS_TOKEN` and gets an unauthenticated
  metrics endpoint anyway.
* A healthcheck polls a route that no longer exists, so the container reports
  unhealthy forever, or `--wait` never returns.
* A Dockerfile COPYs a file that was renamed, which fails the build at the
  worst moment — during a deployment, not during development.
"""
import re
from pathlib import Path

import pytest

from app.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_BACKEND_DOCKERFILE = _REPO_ROOT / "backend" / "Dockerfile"
_FRONTEND_DOCKERFILE = _REPO_ROOT / "frontend" / "Dockerfile"


@pytest.fixture(scope="module")
def compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_dockerfile() -> str:
    return _BACKEND_DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_dockerfile() -> str:
    return _FRONTEND_DOCKERFILE.read_text(encoding="utf-8")


class TestFilesReferencedByTheBuildExist:
    def test_the_deployment_files_are_present(self):
        for path in (_COMPOSE, _BACKEND_DOCKERFILE, _FRONTEND_DOCKERFILE):
            assert path.is_file(), f"{path} is referenced by the documented deployment path"

    def test_each_build_context_exists(self, compose_text):
        contexts = re.findall(r"context:\s*(\S+)", compose_text)
        assert contexts, "no build contexts found — compose no longer builds the services"
        for context in contexts:
            assert (_REPO_ROOT / context).is_dir(), f"build context {context} does not exist"

    @pytest.mark.parametrize("relative", ["backend/.dockerignore", "frontend/.dockerignore"])
    def test_dockerignore_files_exist(self, relative):
        """Without these, the build context includes .venv/node_modules and the
        model weights the backend Dockerfile documents as deliberately absent."""
        assert (_REPO_ROOT / relative).is_file()

    def test_every_file_the_frontend_image_copies_exists(self, frontend_dockerfile):
        """`COPY --from=builder /app/next.config.js` fails the build outright if
        the config is renamed to .ts or .mjs — a rename that is invisible to
        every other check in this repository."""
        copied = re.findall(r"COPY --from=builder /app/([\w./-]+)", frontend_dockerfile)
        for name in copied:
            if name.startswith(".next"):
                continue  # build output, not present in a clean checkout
            assert (_REPO_ROOT / "frontend" / name).exists(), f"frontend/{name} is COPYed by the image but missing"

    def test_the_backend_image_copies_its_requirements(self, backend_dockerfile):
        assert "COPY requirements.txt" in backend_dockerfile
        assert (_REPO_ROOT / "backend" / "requirements.txt").is_file()


class TestVolumesMatchWhereTheAppWrites:
    """The data-loss case: a mount that no longer covers the write path."""

    def _mounted_container_paths(self, compose_text: str) -> set[str]:
        return set(re.findall(r"-\s+\w+:(/\S+)", compose_text))

    def test_evidence_is_written_into_a_mounted_volume(self, compose_text):
        mounted = self._mounted_container_paths(compose_text)
        # The image sets WORKDIR /app and copies the backend there, so the
        # settings default resolves to /app/<name> inside the container.
        assert f"/app/{settings.evidence_dir.name}" in mounted, (
            f"evidence_dir is '{settings.evidence_dir.name}' but compose mounts {sorted(mounted)} — "
            "captured evidence would be lost on the next rebuild"
        )

    def test_uploads_are_written_into_a_mounted_volume(self, compose_text):
        mounted = self._mounted_container_paths(compose_text)
        assert f"/app/{settings.uploads_dir.name}" in mounted, (
            f"uploads_dir is '{settings.uploads_dir.name}' but compose mounts {sorted(mounted)}"
        )

    def test_the_database_has_a_persistent_volume(self, compose_text):
        assert "/var/lib/postgresql/data" in self._mounted_container_paths(compose_text)


class TestEnvironmentKeysAreRead:
    def test_every_backend_environment_key_maps_to_a_setting(self, compose_text):
        """A key compose sets that settings does not read is silently ignored —
        the deployment believes it configured something it did not."""
        backend_block = compose_text.split("backend:", 1)[1].split("frontend:", 1)[0]
        env_block = backend_block.split("environment:", 1)[1].split("volumes:", 1)[0]
        keys = re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):", env_block, flags=re.MULTILINE)
        assert keys, "no environment keys parsed — the compose layout changed"

        known = set(type(settings).model_fields.keys())
        unread = [k for k in keys if k.lower() not in known]
        assert unread == [], f"compose sets keys the application never reads: {unread}"

    def test_the_secrets_have_no_guessable_defaults(self, compose_text):
        """`:?` makes compose FAIL when the value is unset, rather than starting
        a police datastore on a default password."""
        assert "POSTGRES_PASSWORD:?" in compose_text.replace("${", "").replace("}", "")
        assert "JWT_SECRET:?" in compose_text.replace("${", "").replace("}", "")


class TestHealthchecksPollRealRoutes:
    def test_the_backend_healthcheck_route_exists(self, backend_dockerfile):
        match = re.search(r"curl -fsS http://localhost:8000(\S+)", backend_dockerfile)
        assert match, "backend healthcheck no longer curls a route"
        route = match.group(1)

        from app.main import app

        # getattr: app.routes also holds mounted sub-routers, which carry no
        # `.path` attribute at all.
        served = {getattr(r, "path", None) for r in app.routes}
        assert route in served, f"healthcheck polls {route}, which the app does not serve"

    def test_the_frontend_healthcheck_route_exists(self, frontend_dockerfile):
        match = re.search(r"http://localhost:3000(\S+)", frontend_dockerfile)
        assert match
        route = match.group(1).rstrip("'\" ")
        page = _REPO_ROOT / "frontend" / "app" / route.lstrip("/") / "page.tsx"
        assert page.is_file(), f"healthcheck polls {route}, but {page} does not exist"

    def test_the_backend_healthcheck_allows_for_model_loading(self, backend_dockerfile):
        """`start-period` has to exceed a cold start: ultralytics and easyocr
        fetch weights on first use, and a short grace period would restart the
        container in a loop while it is legitimately starting."""
        match = re.search(r"--start-period=(\d+)s", backend_dockerfile)
        assert match and int(match.group(1)) >= 60


class TestSchemaIsMigratedOnStart:
    def test_compose_brings_the_schema_to_head_before_serving(self, compose_text):
        """On PostgreSQL, Alembic owns the schema — app/db.py's additive helpers
        are SQLite-only, so without this the first request hits missing tables."""
        assert "alembic upgrade head" in compose_text
        backend_block = compose_text.split("backend:", 1)[1].split("frontend:", 1)[0]
        command = backend_block.split("command:", 1)[1]
        assert command.index("alembic upgrade head") < command.index("uvicorn"), (
            "the app is started before the migration runs"
        )

    def test_the_backend_service_waits_for_a_healthy_database(self, compose_text):
        assert "condition: service_healthy" in compose_text

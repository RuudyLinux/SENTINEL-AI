"""The compose/Dockerfile contract, checked against the app.

Not a substitute for `docker compose up --build`, the only thing that proves
the images build and start. That can't run here (Docker Desktop's installer
needs an elevation prompt), and the README says so.

What can be checked is where drift hides, since none of it fails loudly:
* a volume mounted where the app no longer writes: evidence ends up in the
  container's writable layer and disappears on the next rebuild, while tests
  and `docker ps` look fine
* a compose env var the settings don't read (rename, typo): the service
  starts on the default, e.g. METRICS_TOKEN set and metrics open anyway
* a healthcheck on a route that's gone: unhealthy forever, --wait hangs
* a Dockerfile COPY of a renamed file: the build breaks mid-deploy
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
        """Otherwise the build context pulls in .venv/node_modules and the
        model weights the Dockerfile says are left out."""
        assert (_REPO_ROOT / relative).is_file()

    @pytest.mark.parametrize("pattern", [".venv-gpu/", ".env", ".env.*", "*.db"])
    def test_backend_image_excludes_local_environments_and_secrets(self, pattern):
        """The CUDA env is several GB and .env.* copies have held the live
        grid password; COPY . . mustn't bring either along."""
        lines = (_REPO_ROOT / "backend/.dockerignore").read_text(encoding="utf-8").splitlines()
        assert pattern in lines

    def test_every_file_the_frontend_image_copies_exists(self, frontend_dockerfile):
        """COPY --from=builder /app/next.config.js breaks the build if the
        config is renamed to .ts or .mjs, and nothing else here would notice."""
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
        """A key compose sets that settings doesn't read is silently ignored."""
        backend_block = compose_text.split("backend:", 1)[1].split("frontend:", 1)[0]
        env_block = backend_block.split("environment:", 1)[1].split("volumes:", 1)[0]
        keys = re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):", env_block, flags=re.MULTILINE)
        assert keys, "no environment keys parsed — the compose layout changed"

        known = set(type(settings).model_fields.keys())
        unread = [k for k in keys if k.lower() not in known]
        assert unread == [], f"compose sets keys the application never reads: {unread}"

    def test_the_secrets_have_no_guessable_defaults(self, compose_text):
        """`:?` makes compose fail when unset instead of starting the DB on a
        default password."""
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
        """start-period has to cover a cold start (ultralytics/easyocr fetch
        weights on first use), or the container restart-loops while starting."""
        match = re.search(r"--start-period=(\d+)s", backend_dockerfile)
        assert match and int(match.group(1)) >= 60


class TestSchemaIsMigratedOnStart:
    def test_compose_brings_the_schema_to_head_before_serving(self, compose_text):
        """On PostgreSQL Alembic owns the schema (db.py's helpers are SQLite
        only), so without this the first request hits missing tables."""
        assert "alembic upgrade head" in compose_text
        backend_block = compose_text.split("backend:", 1)[1].split("frontend:", 1)[0]
        command = backend_block.split("command:", 1)[1]
        assert command.index("alembic upgrade head") < command.index("uvicorn"), (
            "the app is started before the migration runs"
        )

    def test_the_backend_service_waits_for_a_healthy_database(self, compose_text):
        assert "condition: service_healthy" in compose_text

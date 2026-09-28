"""Prometheus metrics endpoint. No unauthenticated mode; the numbers are
operationally sensitive.
"""
import pytest

from app import metrics
from app.config import settings


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class TestAccessControl:
    def test_unauthenticated_scrapes_are_refused(self, client):
        assert client.get("/api/metrics").status_code == 401

    def test_a_garbage_token_is_refused(self, client):
        assert client.get("/api/metrics", headers={"Authorization": "Bearer nonsense"}).status_code == 401

    def test_an_administrator_token_is_accepted(self, client, auth):
        assert client.get("/api/metrics", headers=auth).status_code == 200

    def test_the_configured_scrape_token_is_accepted(self, client, monkeypatch):
        """Prometheus can't do a JWT login, so it needs a shared scrape secret."""
        monkeypatch.setattr(settings, "metrics_token", "a-real-scrape-secret")
        resp = client.get("/api/metrics", headers={"Authorization": "Bearer a-real-scrape-secret"})
        assert resp.status_code == 200

    def test_a_wrong_scrape_token_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(settings, "metrics_token", "a-real-scrape-secret")
        resp = client.get("/api/metrics", headers={"Authorization": "Bearer a-real-scrape-secre"})
        assert resp.status_code == 401

    def test_an_empty_configured_token_never_authorizes(self, client):
        """Default token is empty, and an empty/missing bearer must not match it."""
        assert settings.metrics_token == ""
        assert client.get("/api/metrics", headers={"Authorization": "Bearer "}).status_code == 401


class TestExposition:
    def test_serves_prometheus_text_format(self, client, auth):
        resp = client.get("/api/metrics", headers=auth)
        assert "text/plain" in resp.headers["content-type"]
        assert "# HELP" in resp.text and "# TYPE" in resp.text

    @pytest.mark.parametrize("series", [
        "sentinel_detections_total",
        "sentinel_plate_ocr_attempts_total",
        "sentinel_plate_ocr_accepted_total",
        "sentinel_plate_localized_total",
        "sentinel_vehicle_sightings_total",
        "sentinel_alerts_total",
        "sentinel_incidents_total",
        "sentinel_inference_seconds",
        "sentinel_ocr_seconds",
        "sentinel_db_write_seconds",
        "sentinel_db_lock_retries_total",
        "sentinel_cameras_running",
        "sentinel_websocket_clients",
        "sentinel_process_cpu_percent",
        "sentinel_process_memory_bytes",
    ])
    def test_the_documented_series_are_exported(self, client, auth, series):
        assert series in client.get("/api/metrics", headers=auth).text

    def test_counters_reflect_real_increments(self, client, auth):
        metrics.DETECTIONS_TOTAL.labels(camera_code="C-METRIC", cls="car").inc()
        body = client.get("/api/metrics", headers=auth).text
        assert 'sentinel_detections_total{camera_code="C-METRIC",cls="car"}' in body

    def test_gpu_memory_is_absent_rather_than_zero_without_a_gpu(self, client, auth):
        """0 bytes on a CPU-only host would look like an idle GPU; no CUDA,
        no series."""
        import torch

        if torch.cuda.is_available():
            pytest.skip("a real CUDA device is present; the absent-series case does not apply")
        body = client.get("/api/metrics", headers=auth).text
        assert "sentinel_gpu_memory_allocated_bytes " not in body

    def test_rendering_never_raises_even_with_no_cameras(self):
        """A metrics failure mustn't take the endpoint down, partial beats 500."""
        assert b"sentinel_" in metrics.render()

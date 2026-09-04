"""3. Missing catalogue host — never fabricates cameras, fails clearly."""
import asyncio

import pytest

from app.pipeline.catalog import fetch_catalog, CatalogError


def test_fetch_catalog_raises_clear_error_when_host_unset(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "camera_catalog_base_url", "")
    with pytest.raises(CatalogError, match="CAMERA_CATALOG_BASE_URL is not configured"):
        asyncio.run(fetch_catalog())

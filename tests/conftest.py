"""Test-wide fixtures."""
import pytest

import config


@pytest.fixture(autouse=True)
def default_runtime_config(monkeypatch):
    """Tests assume the default runtime mode, whatever the environment says
    (e.g. inside a container started with INGEST_MODE=redis or the synthetic
    feed). A test that needs another mode sets it explicitly."""
    monkeypatch.setattr(config, "INGEST_MODE", "embedded")
    monkeypatch.setattr(config, "MARKET_DATA_PROVIDER", "angelone")
    monkeypatch.setattr(config, "SYNTHETIC_FEED", False)

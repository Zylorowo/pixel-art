from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pixelwar.config import Settings
from pixelwar.main import create_app

PALETTE = ("#FFFFFF", "#FF0000", "#00FF00", "#0000FF")


def make_settings(tmp_path, **overrides) -> Settings:
    now = datetime.now(timezone.utc)
    values = dict(
        db_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
        dev_mode=True,
        secret_key="test-secret",
        event_start=now - timedelta(days=1),
        event_end=now + timedelta(days=1),
        palette=PALETTE,
        # Tests malen viel schneller als ein Mensch.
        pixels_per_second=1000,
        # Hintergrundjobs in Tests schnell laufen lassen.
        flush_interval_s=0.02,
        cooldown_interval_s=3600,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def login(client, handle="alice", topup=None):
    r = client.post("/api/dev/login", json={"handle": handle})
    assert r.status_code == 200, r.text
    if topup:
        r = client.post("/api/topup", json={"amount_cents": topup, "age_confirmed": True})
        assert r.status_code == 200, r.text
    return client

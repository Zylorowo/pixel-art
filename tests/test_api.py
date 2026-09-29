"""Tests über die HTTP-/WebSocket-Schnittstelle."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pixelkrieg.main import create_app

from .conftest import login, make_settings


def test_login_me_topup(client):
    assert client.get("/api/me").status_code == 401
    login(client, "alice")
    me = client.get("/api/me").json()
    assert me["handle"] == "alice" and me["balance_cents"] == 0

    r = client.post("/api/topup", json={"amount_cents": 1000})
    assert r.status_code == 400 and r.json()["error"] == "age_not_confirmed"
    r = client.post("/api/topup", json={"amount_cents": 1234, "age_confirmed": True})
    assert r.json()["error"] == "bad_package"

    r = client.post("/api/topup", json={"amount_cents": 1000, "age_confirmed": True})
    assert r.status_code == 200 and r.json()["balance_cents"] == 1000

    # Obergrenze 25 €: nach 10 + 10 passt nur noch das 3-€-Paket
    client.post("/api/topup", json={"amount_cents": 1000, "age_confirmed": True})
    me = client.get("/api/me").json()
    assert me["topup_packages_cents"] == [300]
    r = client.post("/api/topup", json={"amount_cents": 1000, "age_confirmed": True})
    assert r.json()["error"] == "topup_cap"


def test_forged_cookie_rejected(client):
    client.cookies.set("pk_session", "1.deadbeef")
    assert client.get("/api/me").status_code == 401


def test_paint_flow_and_zones(client):
    login(client, "alice", topup=300)
    r = client.post("/api/paint", json={"x": 3, "y": 4, "color_index": 1, "max_price_cents": 2})
    assert r.status_code == 200, r.text
    assert r.json() == {"price_cents": 2, "balance_cents": 298, "zone_price_cents": 2, "heat": 0}

    r = client.post("/api/paint", json={"x": 3, "y": 4, "color_index": 2})
    assert r.json()["heat"] == 1

    zones = client.get("/api/zones").json()
    assert zones["heat"][0] == 1 and zones["prices"][0] == 2
    assert len(zones["prices"]) == 400

    canvas = client.get("/api/canvas")
    assert canvas.headers["content-encoding"] == "gzip"
    data = canvas.content  # httpx entpackt automatisch
    assert len(data) == 400 * 400
    assert data[4 * 400 + 3] == 2


def test_paint_requires_login_and_funds(client):
    r = client.post("/api/paint", json={"x": 0, "y": 0, "color_index": 1})
    assert r.status_code == 401
    login(client, "bob")
    r = client.post("/api/paint", json={"x": 0, "y": 0, "color_index": 1})
    assert r.status_code == 402 and r.json()["error"] == "insufficient_funds"


def test_rate_limit(tmp_path):
    app = create_app(make_settings(tmp_path, pixels_per_second=5))
    with TestClient(app) as c:
        login(c, "alice", topup=300)
        codes = [
            c.post("/api/paint", json={"x": i, "y": 0, "color_index": 1}).status_code for i in range(8)
        ]
    assert codes.count(200) == 5
    assert codes.count(429) == 3


def test_event_not_started(tmp_path):
    now = datetime.now(timezone.utc)
    app = create_app(make_settings(tmp_path, event_start=now + timedelta(hours=1), event_end=now + timedelta(days=1)))
    with TestClient(app) as c:
        login(c, "alice")
        r = c.post("/api/topup", json={"amount_cents": 300, "age_confirmed": True})
        assert r.status_code == 403 and r.json()["error"] == "event_before"
        assert c.get("/api/config").json()["event"]["phase"] == "before"


def test_fake_webhook_signature_and_idempotency(client):
    login(client, "alice")
    uid = client.get("/api/me").json()["id"]
    provider = client.app.state.providers["fake"]
    provider.create_checkout(uid, 300)
    [(body, headers)] = provider.pop_webhooks()

    assert client.post("/api/webhooks/fake", content=body, headers={"x-fake-signature": "x"}).status_code == 400
    r1 = client.post("/api/webhooks/fake", content=body, headers=headers)
    r2 = client.post("/api/webhooks/fake", content=body, headers=headers)
    assert r1.json()["status"] == "paid"
    assert r2.json()["status"] == "duplicate"
    assert client.get("/api/me").json()["balance_cents"] == 300


def test_no_fake_provider_without_dev_mode(tmp_path):
    app = create_app(make_settings(tmp_path, dev_mode=False))
    with TestClient(app) as c:
        assert c.post("/api/dev/login", json={"handle": "x"}).status_code == 404
        assert c.post("/api/webhooks/fake", content=b"{}").status_code == 404


def test_websocket_receives_batched_pixels(client):
    login(client, "alice", topup=300)
    with client.websocket_connect("/ws") as ws:
        v0 = int(client.get("/api/canvas").headers["x-canvas-version"])
        for x in range(3):
            client.post("/api/paint", json={"x": x, "y": 7, "color_index": 3})
        received = {}
        while len(received) < 3:
            msg = ws.receive_json()
            assert msg["v"] > v0
            if msg["type"] == "pixel":
                d = msg["data"]
                for i in range(0, len(d), 3):
                    received[(d[i], d[i + 1])] = d[i + 2]
    assert received == {(0, 7): 3, (1, 7): 3, (2, 7): 3}

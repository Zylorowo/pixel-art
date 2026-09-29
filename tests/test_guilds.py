"""Gilden: gründen, beitreten, folgen, Bild."""

import io

from fastapi.testclient import TestClient
from PIL import Image

from pixelwar.main import create_app

from .conftest import login, make_settings

GUILD = {"slug": "team-blau", "name": "Team Blau", "description": "Wir malen blau.", "color_hex": "#3366ff"}


def test_create_costs_five_euros_and_makes_founder_member(client):
    login(client, "gruender")
    r = client.post("/api/guilds", json=GUILD)
    assert r.status_code == 402 and r.json()["error"] == "insufficient_funds"

    client.post("/api/topup", json={"amount_cents": 1000, "age_confirmed": True})
    r = client.post("/api/guilds", json=GUILD)
    assert r.status_code == 200, r.text
    assert r.json()["balance_cents"] == 500
    me = client.get("/api/me").json()
    assert me["guild"]["slug"] == "team-blau" and me["guild"]["color"] == "#3366FF"

    # Zweite Gilde gründen geht nicht (kein Wechsel)
    r = client.post("/api/guilds", json={**GUILD, "slug": "team-rot"})
    assert r.json()["error"] == "already_in_guild"
    assert client.get("/api/me").json()["balance_cents"] == 500  # nichts abgebucht


def test_validation(client):
    login(client, "gg", topup=1000)
    for bad in [{"slug": "A B"}, {"slug": "ab"}, {"slug": "admin"}, {"name": "x"}, {"color_hex": "blau"}]:
        r = client.post("/api/guilds", json={**GUILD, **bad})
        assert r.status_code == 400, bad
    assert client.get("/api/me").json()["balance_cents"] == 1000


def test_join_follow_no_switching(client):
    login(client, "gruender", topup=1000)
    client.post("/api/guilds", json=GUILD)

    login(client, "rot", topup=1000)
    client.post("/api/guilds", json={**GUILD, "slug": "team-rot", "name": "Team Rot"})

    fan = login(client, "fan")  # Login wechselt den Nutzer im selben Client
    # Folgen ohne Mitgliedschaft
    assert fan.post("/api/guilds/team-blau/follow").status_code == 200
    g = fan.get("/api/guilds/team-blau").json()
    assert (g["members"], g["followers"]) == (1, 1)
    assert g["viewer"] == {"is_member": False, "in_other_guild": False, "is_following": True, "is_owner": False}

    # Beitritt per Link, ein Klick; zweimal klicken ist harmlos
    assert fan.post("/api/guilds/team-blau/join").status_code == 200
    assert fan.post("/api/guilds/team-blau/join").status_code == 200
    assert fan.get("/api/guilds/team-blau").json()["members"] == 2

    # Andere Gilde: Wechsel verboten
    r = fan.post("/api/guilds/team-rot/join")
    assert r.status_code == 409 and r.json()["error"] == "already_in_guild"
    # Folgen darf man trotzdem
    assert fan.post("/api/guilds/team-rot/follow").status_code == 200

    fan.delete("/api/guilds/team-blau/follow")
    assert fan.get("/api/guilds/team-blau").json()["followers"] == 0


def test_held_pixels_follow_last_painter(client):
    login(client, "blau", topup=2500)
    client.post("/api/guilds", json=GUILD)
    for x in range(3):
        client.post("/api/paint", json={"x": x, "y": 0, "color_index": 1})
    assert client.get("/api/guilds/team-blau").json()["held_pixels"] == 3

    login(client, "rot", topup=2500)
    client.post("/api/guilds", json={**GUILD, "slug": "team-rot", "name": "Team Rot"})
    client.post("/api/paint", json={"x": 0, "y": 0, "color_index": 2})  # übermalt ein blaues Pixel
    assert client.get("/api/guilds/team-blau").json()["held_pixels"] == 2
    listing = client.get("/api/guilds").json()["guilds"]
    assert [(g["slug"], g["held_pixels"]) for g in listing] == [("team-blau", 2), ("team-rot", 1)]

    info = client.get("/api/pixel", params={"x": 0, "y": 0}).json()
    assert info["guild"]["slug"] == "team-rot" and info["user"] == "rot"


def test_owner_edits_and_image_upload(client):
    login(client, "gruender", topup=1000)
    client.post("/api/guilds", json=GUILD)
    assert client.patch("/api/guilds/team-blau", json={"description": "Neu", "color_hex": "#00ff00"}).status_code == 200
    g = client.get("/api/guilds/team-blau").json()
    assert g["description"] == "Neu" and g["color"] == "#00FF00" and g["image_url"] is None

    buf = io.BytesIO()
    Image.new("RGB", (800, 400), "red").save(buf, "JPEG")
    r = client.put("/api/guilds/team-blau/image", content=buf.getvalue())
    assert r.status_code == 200, r.text
    img = client.get(r.json()["image_url"])
    assert img.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(img.content)).size == (256, 256)

    assert client.put("/api/guilds/team-blau/image", content=b"kein bild").json()["error"] == "bad_image"

    login(client, "fremd")
    assert client.patch("/api/guilds/team-blau", json={"description": "gehackt"}).status_code == 403


def test_join_link_page_and_unknown_guild(client):
    assert client.get("/c/team-blau").status_code == 200  # HTML-Seite, JS lädt die Daten
    assert client.get("/api/guilds/gibtsnicht").status_code == 404


def test_image_size_limit(tmp_path):
    app = create_app(make_settings(tmp_path, guild_image_max_bytes=1000))
    with TestClient(app) as c:
        login(c, "gg", topup=1000)
        c.post("/api/guilds", json=GUILD)
        assert c.put("/api/guilds/team-blau/image", content=b"x" * 2000).status_code == 413

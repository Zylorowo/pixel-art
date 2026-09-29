"""FastAPI-App: HTTP-Endpunkte, WebSocket, Hintergrund-Jobs.

Starten (Entwicklung):
    uvicorn pixelwar.main:create_app --factory --reload

Kurz zu FastAPI für Einsteiger:
- `@app.get("/pfad")` hängt eine Funktion an eine URL.
- Funktionsparameter mit Pydantic-Modell (z. B. `req: PaintRequest`) werden
  automatisch aus dem JSON-Body gelesen und geprüft. Falscher Typ → 422-Fehler,
  ohne dass wir eine Zeile Prüfcode schreiben.
- `Depends(current_user_id)` heißt: "Rufe vorher current_user_id auf und gib mir
  das Ergebnis". So steht die Login-Prüfung an einer Stelle statt in jedem Endpunkt.
- `async def` + `await`: Während eine Anfrage wartet (Datenbank, Netzwerk),
  kann der Server andere Anfragen bearbeiten.

Fehlermeldungen: Der Server schickt einen maschinenlesbaren `error`-Code. Das
Frontend übersetzt ihn über die Sprachdatei. `message` ist nur ein englischer
Rückfall für Werkzeuge wie curl.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hmac
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, guilds
from .config import Settings, load_settings
from .event import RUNNING, event_phase
from .game import Game, GameError
from .payments import FakeProvider, InvalidWebhook, PaymentProvider
from .pricing import PriceParams
from .ratelimit import SlidingWindowLimiter
from .realtime import Hub

log = logging.getLogger("pixelwar")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_\-]{2,24}$")


class PaintRequest(BaseModel):
    x: int
    y: int
    color_index: int
    # Der Preis, den der Nutzer angezeigt bekommen hat. Optional, aber das
    # Frontend schickt ihn immer mit (siehe Game._paint_tx).
    max_price_cents: int | None = None


class TopupRequest(BaseModel):
    amount_cents: int
    # Checkbox "Ich bin 18 oder habe die Erlaubnis meiner Eltern".
    age_confirmed: bool = False


class DevLoginRequest(BaseModel):
    handle: str


class GuildCreateRequest(BaseModel):
    slug: str
    name: str
    description: str = ""
    color_hex: str


class GuildUpdateRequest(BaseModel):
    description: str | None = None
    color_hex: str | None = None


class ParamsRequest(BaseModel):
    base_cents: int
    factor: float
    contagion: float
    cooldown_minutes: int


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    game = Game(settings)
    hub = Hub(settings.flush_interval_s, settings.client_queue_size)
    limiter = SlidingWindowLimiter(settings.pixels_per_second, 1.0)
    basic = HTTPBasic(realm="Pixel War Admin")
    providers: dict[str, PaymentProvider] = {}
    if settings.dev_mode:
        # Der FakeProvider existiert NUR im Entwicklungsmodus. Ohne dev_mode ist
        # er gar nicht registriert, also auch nicht per Webhook-URL erreichbar.
        providers["fake"] = FakeProvider(settings.secret_key)

    async def cooldown_loop() -> None:
        while True:
            await asyncio.sleep(settings.cooldown_interval_s)
            try:
                for x, y, price in await game.cool_down():
                    hub.queue_price(x, y, price)
            except Exception:
                log.exception("Abkühlung fehlgeschlagen")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Läuft einmal beim Start (vor `yield`) und beim Herunterfahren (danach).
        game.open()
        tasks = [asyncio.create_task(hub.run()), asyncio.create_task(cooldown_loop())]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            game.close()

    app = FastAPI(title="Pixel War", lifespan=lifespan)
    # Für Tests und Admin-Werkzeuge erreichbar machen.
    app.state.settings = settings
    app.state.game = game
    app.state.hub = hub
    app.state.providers = providers

    @app.exception_handler(GameError)
    async def game_error_handler(request: Request, exc: GameError):
        return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)

    # ------------------------------------------------------------------ Hilfen

    def optional_user_id(request: Request) -> int | None:
        uid = auth.read_token(settings.secret_key, request.cookies.get(auth.COOKIE_NAME))
        if uid is None or game.get_user(uid) is None:
            return None
        return uid

    def current_user_id(uid: int | None = Depends(optional_user_id)) -> int:
        if uid is None:
            raise GameError("not_logged_in", "Please log in", 401)
        return uid

    def require_admin(creds: HTTPBasicCredentials = Depends(basic)) -> None:
        # HTTP Basic Auth: Der Browser fragt selbst nach Name und Passwort.
        # Unabhängig vom Spieler-Login, damit ein Spieler-Account nie Admin wird.
        ok = hmac.compare_digest(creds.username, "admin") & hmac.compare_digest(
            creds.password.encode(), settings.admin_password.encode()
        )
        if not ok:
            # Der Header WWW-Authenticate sorgt dafür, dass der Browser erneut fragt.
            raise HTTPException(401, "Wrong admin password", headers={"WWW-Authenticate": "Basic"})

    def require_running() -> None:
        phase = event_phase(settings)
        if phase != RUNNING:
            raise GameError("event_" + phase, f"Event is not running ({phase})", 403)

    async def process_webhook(provider: PaymentProvider, body: bytes, headers: dict) -> dict:
        try:
            result = provider.verify_webhook(body, headers)
        except InvalidWebhook as e:
            raise GameError("invalid_webhook", str(e), 400)
        if not result.paid:
            return {"status": "ignored"}
        outcome = await game.credit_topup(provider.name, result.provider_ref, result.user_id, result.amount_cents)
        if outcome.status == "refund_needed":
            log.warning("Aufladung über Limit, Rückerstattung nötig: %s/%s", provider.name, result.provider_ref)
        return {"status": outcome.status, "balance_cents": outcome.balance_cents}

    def guild_summary(g: dict) -> dict:
        return {
            "slug": g["slug"], "name": g["name"], "color": g["color_hex"],
            "image_url": f"/api/guilds/{g['slug']}/image?v={g['image_version']}" if g["image_version"] else None,
        }

    def guild_for_user(user: dict) -> dict | None:
        if not user["guild_id"]:
            return None
        row = game.reader.execute("SELECT * FROM guilds WHERE id = ?", (user["guild_id"],)).fetchone()
        return guild_summary(dict(row)) if row else None

    # Schnappschüsse werden pro Version nur einmal komprimiert, egal wie viele
    # Browser sie gleichzeitig abrufen (z. B. 2.000 nach einem Stream-Aufruf).
    snapshot_cache: dict[str, tuple[int, bytes]] = {}

    def snapshot(name: str, make_bytes) -> Response:
        cached = snapshot_cache.get(name)
        if cached is None or cached[0] != hub.version:
            cached = (hub.version, gzip.compress(make_bytes(), compresslevel=6))
            snapshot_cache[name] = cached
        return Response(
            content=cached[1],
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "no-store",
                "X-Version": str(cached[0]),
                "X-Canvas-Size": str(settings.canvas_size),
            },
        )

    # ------------------------------------------------------------------ Seiten

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/guilds", include_in_schema=False)
    @app.get("/c/{slug}", include_in_schema=False)
    async def guild_page(slug: str | None = None):
        # Eine HTML-Seite für Liste und Einzelseite; das JavaScript liest den Pfad.
        return FileResponse(STATIC_DIR / "guild.html")

    @app.get("/admin", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def admin_page():
        return FileResponse(STATIC_DIR / "admin.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ Leinwand & Preise

    @app.get("/api/config")
    async def api_config():
        return {
            "canvas_size": settings.canvas_size,
            "palette": list(settings.palette),
            "event": {
                "phase": event_phase(settings),
                "start": settings.event_start.isoformat(),
                "end": settings.event_end.isoformat(),
                "server_now": datetime.now(timezone.utc).isoformat(),
            },
            "guild_creation_cents": settings.guild_creation_cents,
            "dev_mode": settings.dev_mode,
        }

    @app.get("/api/canvas")
    async def api_canvas():
        # Binärformat: 1 Byte pro Pixel (Farbindex), Zeile für Zeile, gzip-komprimiert.
        # 160.000 Bytes roh, komprimiert meist nur wenige KB. JSON wäre ~5× größer.
        # Der Browser entpackt gzip automatisch (Content-Encoding).
        return snapshot("canvas", game.canvas.tobytes)

    @app.get("/api/prices")
    async def api_prices():
        # 4 Bytes pro Pixel (uint32, little-endian), Preis in Cent. Da fast alle
        # Pixel denselben Grundpreis haben, komprimiert gzip das sehr stark.
        return snapshot("prices", lambda: game.prices.astype("<u4").tobytes())

    @app.get("/api/pixel")
    async def api_pixel(x: int, y: int):
        n = settings.canvas_size
        if not (0 <= x < n and 0 <= y < n):
            raise GameError("out_of_bounds", "Pixel is outside the canvas")
        return game.pixel_info(x, y)

    @app.post("/api/paint")
    async def api_paint(req: PaintRequest, user_id: int = Depends(current_user_id)):
        require_running()
        # Rate-Limit VOR der Datenbank: zählt jeden Versuch, auch fehlgeschlagene,
        # damit niemand den Server mit Anfragen zuschütten kann.
        if not limiter.allow(user_id):
            raise GameError("rate_limited", "Too fast", 429, limit=settings.pixels_per_second)
        result = await game.paint(user_id, req.x, req.y, req.color_index, req.max_price_cents)
        hub.queue_pixel(result.x, result.y, result.color_index)
        for x, y, price in result.price_changes:
            hub.queue_price(x, y, price)
        return {
            "price_cents": result.price_cents,
            "balance_cents": result.balance_cents,
            "new_price_cents": int(game.prices[result.y, result.x]),
        }

    # ------------------------------------------------------------------ Account & Guthaben

    @app.get("/api/me")
    async def api_me(user_id: int = Depends(current_user_id)):
        user = game.get_user(user_id)
        total = game.total_topup_cents(user_id)
        return {
            "id": user["id"],
            "handle": user["handle"],
            "balance_cents": user["balance_cents"],
            "is_banned": bool(user["is_banned"]),
            "guild": guild_for_user(user),
            "total_topup_cents": total,
            "topup_cap_cents": settings.max_total_topup_cents,
            # Nur Pakete anbieten, die noch unter die Obergrenze passen.
            "topup_packages_cents": [
                p for p in settings.topup_packages_cents if total + p <= settings.max_total_topup_cents
            ],
        }

    @app.post("/api/topup")
    async def api_topup(req: TopupRequest, user_id: int = Depends(current_user_id)):
        require_running()
        if not req.age_confirmed:
            raise GameError("age_not_confirmed", "Please confirm your age")
        if req.amount_cents not in settings.topup_packages_cents:
            raise GameError("bad_package", "Unknown package")
        if game.get_user(user_id)["is_banned"]:
            raise GameError("banned", "This account is banned", 403)
        if game.total_topup_cents(user_id) + req.amount_cents > settings.max_total_topup_cents:
            raise GameError("topup_cap", "Top-up limit reached", cap_cents=settings.max_total_topup_cents)

        # Phase 1: Es gibt nur den FakeProvider. In Phase 4 wird hier der
        # gewünschte Anbieter (Stripe/PayPal) ausgewählt.
        provider = providers.get("fake")
        if provider is None:
            raise GameError("no_provider", "Top-ups are not available yet", 503)
        url = provider.create_checkout(user_id, req.amount_cents)

        # Der FakeProvider "zahlt" sofort: seine Webhooks gleich verarbeiten,
        # über denselben Weg wie ein echter Webhook.
        for body, headers in provider.pop_webhooks():
            await process_webhook(provider, body, headers)
        return {"checkout_url": url, "completed": True, "balance_cents": game.get_user(user_id)["balance_cents"]}

    @app.post("/api/webhooks/{provider_name}")
    async def api_webhook(provider_name: str, request: Request):
        provider = providers.get(provider_name)
        if provider is None:
            raise GameError("unknown_provider", "Unknown payment provider", 404)
        return await process_webhook(provider, await request.body(), dict(request.headers))

    # ------------------------------------------------------------------ Gilden

    @app.get("/api/guilds")
    async def api_guilds():
        items = []
        for g in guilds.list_guilds(game.reader):
            items.append({**guild_summary(g), "members": g["members"], "followers": g["followers"],
                          "held_pixels": game.held_pixels(g["id"])})
        items.sort(key=lambda g: (-g["held_pixels"], -g["members"]))
        return {"guilds": items}

    @app.post("/api/guilds")
    async def api_guild_create(req: GuildCreateRequest, user_id: int = Depends(current_user_id)):
        require_running()  # kostet Geld → nur während des Events
        guilds.validate_guild_fields(req.slug, req.name, req.description, req.color_hex)
        await game.write(guilds.create_guild_tx, user_id, req.slug, req.name, req.description,
                         req.color_hex, settings.guild_creation_cents)
        return {"slug": req.slug, "balance_cents": game.get_user(user_id)["balance_cents"]}

    @app.get("/api/guilds/{slug}")
    async def api_guild(slug: str, user_id: int | None = Depends(optional_user_id)):
        g = guilds.get_guild(game.reader, slug)
        members, followers = guilds.guild_counts(game.reader, g["id"])
        held = game.held_pixels(g["id"])
        user = game.get_user(user_id) if user_id else None
        return {
            **guild_summary(g),
            "description": g["description"],
            "owner": g["owner_handle"],
            "members": members,
            "followers": followers,
            "held_pixels": held,
            "held_percent": round(100 * held / settings.canvas_size**2, 2),
            "created_at": g["created_at"],
            "viewer": None if user is None else {
                "is_member": user["guild_id"] == g["id"],
                "in_other_guild": user["guild_id"] is not None and user["guild_id"] != g["id"],
                "is_following": guilds.is_following(game.reader, g["id"], user_id),
                "is_owner": g["owner_user_id"] == user_id,
            },
        }

    @app.post("/api/guilds/{slug}/join")
    async def api_guild_join(slug: str, user_id: int = Depends(current_user_id)):
        g = guilds.get_guild(game.reader, slug)
        await game.write(guilds.join_guild_tx, user_id, g["id"])
        return {"ok": True}

    @app.post("/api/guilds/{slug}/follow")
    async def api_guild_follow(slug: str, user_id: int = Depends(current_user_id)):
        g = guilds.get_guild(game.reader, slug)
        await game.write(guilds.follow_tx, user_id, g["id"], True)
        return {"ok": True}

    @app.delete("/api/guilds/{slug}/follow")
    async def api_guild_unfollow(slug: str, user_id: int = Depends(current_user_id)):
        g = guilds.get_guild(game.reader, slug)
        await game.write(guilds.follow_tx, user_id, g["id"], False)
        return {"ok": True}

    def owned_guild(slug: str, user_id: int) -> dict:
        g = guilds.get_guild(game.reader, slug)
        if g["owner_user_id"] != user_id:
            raise GameError("not_owner", "Only the guild founder can do this", 403)
        return g

    @app.patch("/api/guilds/{slug}")
    async def api_guild_update(slug: str, req: GuildUpdateRequest, user_id: int = Depends(current_user_id)):
        g = owned_guild(slug, user_id)
        guilds.validate_guild_fields(None, None, req.description, req.color_hex)
        await game.write(guilds.update_guild_tx, g["id"], req.description, req.color_hex)
        return {"ok": True}

    @app.put("/api/guilds/{slug}/image")
    async def api_guild_image(slug: str, request: Request, user_id: int = Depends(current_user_id)):
        # Das Bild kommt als roher Request-Body (kein Formular), das spart eine
        # zusätzliche Bibliothek für multipart-Uploads.
        g = owned_guild(slug, user_id)
        raw = await request.body()
        if len(raw) > settings.guild_image_max_bytes:
            raise GameError("image_too_large", "Image is too large", 413,
                            max_kb=settings.guild_image_max_bytes // 1024)
        # Bildbearbeitung braucht Rechenzeit → in einen Hilfsthread.
        await asyncio.to_thread(guilds.process_image, raw, guilds.image_path(settings.data_dir, g["id"]))
        version = await game.write(guilds.bump_image_version_tx, g["id"])
        return {"image_url": f"/api/guilds/{slug}/image?v={version}"}

    @app.get("/api/guilds/{slug}/image")
    async def api_guild_image_get(slug: str):
        g = guilds.get_guild(game.reader, slug)
        path = guilds.image_path(settings.data_dir, g["id"])
        if not g["image_version"] or not path.exists():
            raise GameError("no_image", "No image", 404)
        # Die URL enthält ?v=<version>, deshalb darf der Browser lange cachen.
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    # ------------------------------------------------------------------ Admin

    @app.get("/api/admin/params", dependencies=[Depends(require_admin)])
    async def api_admin_params():
        return {"params": game.params.as_dict(), "defaults": settings.default_params.as_dict()}

    @app.put("/api/admin/params", dependencies=[Depends(require_admin)])
    async def api_admin_set_params(req: ParamsRequest):
        params = PriceParams(**req.model_dump())
        try:
            params.validate()
        except ValueError as e:
            raise GameError("bad_params", str(e))
        await game.set_params(params)
        hub.queue_prices_reset()  # alle Browser laden die Preise neu
        log.warning("Preisparameter geändert: %s", params.as_dict())
        return {"params": params.as_dict()}

    # ------------------------------------------------------------------ Login

    @app.post("/api/dev/login")
    async def api_dev_login(req: DevLoginRequest, response: Response):
        if not settings.dev_mode:
            raise GameError("not_found", "Not found", 404)
        if not HANDLE_RE.match(req.handle):
            raise GameError("bad_handle", "Name: 2–24 characters, letters, digits, _ and -")
        uid = await game.get_or_create_user(req.handle)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.make_token(settings.secret_key, uid),
            httponly=True,  # für JavaScript unsichtbar → schwerer zu stehlen
            samesite="lax",  # fremde Seiten können keine POSTs mit dem Cookie auslösen
            secure=not settings.dev_mode,  # in Produktion nur über HTTPS
            max_age=60 * 60 * 24 * 60,
        )
        return {"id": uid}

    @app.post("/api/logout")
    async def api_logout(response: Response):
        response.delete_cookie(auth.COOKIE_NAME)
        return {"ok": True}

    # ------------------------------------------------------------------ WebSocket

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.serve(ws)

    return app

"""FastAPI-App: HTTP-Endpunkte, WebSocket, Hintergrund-Jobs.

Starten (Entwicklung):
    uvicorn pixelkrieg.main:create_app --factory --reload

Kurz zu FastAPI für Einsteiger:
- `@app.get("/pfad")` hängt eine Funktion an eine URL.
- Funktionsparameter mit Pydantic-Modell (z. B. `req: PaintRequest`) werden
  automatisch aus dem JSON-Body gelesen und geprüft. Falscher Typ → 422-Fehler,
  ohne dass wir eine Zeile Prüfcode schreiben.
- `Depends(current_user)` heißt: "Rufe vorher current_user auf und gib mir das
  Ergebnis". So steht die Login-Prüfung an einer Stelle statt in jedem Endpunkt.
- `async def` + `await`: Während eine Anfrage wartet (Datenbank, Netzwerk),
  kann der Server andere Anfragen bearbeiten.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from .config import Settings, load_settings
from .event import RUNNING, event_phase
from .game import Game, GameError
from .payments import FakeProvider, InvalidWebhook, PaymentProvider
from .ratelimit import SlidingWindowLimiter
from .realtime import Hub

log = logging.getLogger("pixelkrieg")
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    game = Game(settings)
    hub = Hub(settings.flush_interval_s, settings.client_queue_size)
    limiter = SlidingWindowLimiter(settings.pixels_per_second, 1.0)
    providers: dict[str, PaymentProvider] = {}
    if settings.dev_mode:
        # Der FakeProvider existiert NUR im Entwicklungsmodus. Ohne dev_mode ist
        # er gar nicht registriert, also auch nicht per Webhook-URL erreichbar.
        providers["fake"] = FakeProvider(settings.secret_key)

    async def cooldown_loop() -> None:
        while True:
            await asyncio.sleep(settings.cooldown_interval_s)
            try:
                for c in await game.cool_down():
                    hub.queue_zone(c.zx, c.zy, c.price_cents, c.heat)
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

    app = FastAPI(title="Pixelkrieg", lifespan=lifespan)
    # Für Tests und Admin-Werkzeuge erreichbar machen.
    app.state.settings = settings
    app.state.game = game
    app.state.hub = hub
    app.state.providers = providers

    @app.exception_handler(GameError)
    async def game_error_handler(request: Request, exc: GameError):
        return JSONResponse(
            {"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status
        )

    # ------------------------------------------------------------------ Hilfen

    def current_user_id(request: Request) -> int:
        uid = auth.read_token(settings.secret_key, request.cookies.get(auth.COOKIE_NAME))
        if uid is None or game.get_user(uid) is None:
            raise GameError("not_logged_in", "Bitte einloggen", 401)
        return uid

    def require_running() -> None:
        phase = event_phase(settings)
        if phase != RUNNING:
            messages = {
                "before": "Das Event hat noch nicht begonnen",
                "night_pause": "Nachtpause – morgen früh geht es weiter",
                "ended": "Das Event ist vorbei",
            }
            raise GameError("event_" + phase, messages[phase], 403)

    async def process_webhook(provider: PaymentProvider, body: bytes, headers: dict) -> dict:
        try:
            result = provider.verify_webhook(body, headers)
        except InvalidWebhook as e:
            raise GameError("invalid_webhook", str(e), 400)
        if not result.paid:
            return {"status": "ignored"}
        outcome = await game.credit_topup(
            provider.name, result.provider_ref, result.user_id, result.amount_cents
        )
        if outcome.status == "refund_needed":
            log.warning("Aufladung über Limit, Rückerstattung nötig: %s/%s", provider.name, result.provider_ref)
        return {"status": outcome.status, "balance_cents": outcome.balance_cents}

    # Der Schnappschuss wird pro Version nur einmal komprimiert, egal wie viele
    # Browser ihn gleichzeitig abrufen (z. B. 2.000 nach einem Stream-Aufruf).
    snapshot_cache: dict = {"version": None, "data": b""}

    # ------------------------------------------------------------------ Seiten

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ Öffentliche API

    @app.get("/api/config")
    async def api_config():
        return {
            "canvas_size": settings.canvas_size,
            "zone_size": settings.zone_size,
            "palette": list(settings.palette),
            "event": {
                "phase": event_phase(settings),
                "start": settings.event_start.isoformat(),
                "end": settings.event_end.isoformat(),
                "server_now": datetime.now(timezone.utc).isoformat(),
            },
            "dev_mode": settings.dev_mode,
        }

    @app.get("/api/canvas")
    async def api_canvas():
        # Binärformat: 1 Byte pro Pixel (Farbindex), Zeile für Zeile, gzip-komprimiert.
        # 160.000 Bytes roh, komprimiert meist nur wenige KB. JSON wäre ~5× größer.
        # Der Browser entpackt gzip automatisch (Content-Encoding).
        if snapshot_cache["version"] != hub.version:
            snapshot_cache["data"] = gzip.compress(game.canvas.tobytes(), compresslevel=6)
            snapshot_cache["version"] = hub.version
        return Response(
            content=snapshot_cache["data"],
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "no-store",
                "X-Canvas-Version": str(snapshot_cache["version"]),
                "X-Canvas-Size": str(settings.canvas_size),
            },
        )

    @app.get("/api/zones")
    async def api_zones():
        # Reihenfolge Zeile für Zeile: Index = zy * zones_per_side + zx
        heat = game.heat.flatten().tolist()
        return {
            "version": hub.version,
            "zones_per_side": settings.zones_per_side,
            "prices": [game.price_for_heat(h) for h in heat],
            "heat": heat,
            "frozen": game.frozen.flatten().astype(int).tolist(),
        }

    @app.post("/api/paint")
    async def api_paint(req: PaintRequest, user_id: int = Depends(current_user_id)):
        require_running()
        # Rate-Limit VOR der Datenbank: zählt jeden Versuch, auch fehlgeschlagene,
        # damit niemand den Server mit Anfragen zuschütten kann.
        if not limiter.allow(user_id):
            raise GameError("rate_limited", "Langsamer! Höchstens %d Pixel pro Sekunde" % settings.pixels_per_second, 429)
        result = await game.paint(user_id, req.x, req.y, req.color_index, req.max_price_cents)
        hub.queue_pixel(result.x, result.y, result.color_index)
        hub.queue_zone(result.zx, result.zy, result.zone_price_cents, result.heat)
        return {
            "price_cents": result.price_cents,
            "balance_cents": result.balance_cents,
            "zone_price_cents": result.zone_price_cents,
            "heat": result.heat,
        }

    @app.get("/api/me")
    async def api_me(user_id: int = Depends(current_user_id)):
        user = game.get_user(user_id)
        total = game.total_topup_cents(user_id)
        return {
            "id": user["id"],
            "handle": user["handle"],
            "balance_cents": user["balance_cents"],
            "is_banned": bool(user["is_banned"]),
            "clan_id": user["clan_id"],
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
            raise GameError(
                "age_not_confirmed",
                "Bitte bestätigen: Du bist 18 oder hast die Erlaubnis deiner Eltern.",
            )
        if req.amount_cents not in settings.topup_packages_cents:
            raise GameError("bad_package", "Dieses Paket gibt es nicht")
        user = game.get_user(user_id)
        if user["is_banned"]:
            raise GameError("banned", "Dieser Account ist gesperrt", 403)
        if game.total_topup_cents(user_id) + req.amount_cents > settings.max_total_topup_cents:
            raise GameError(
                "topup_cap",
                "Obergrenze erreicht: insgesamt höchstens %s € pro Account"
                % f"{settings.max_total_topup_cents / 100:.2f}".replace(".", ","),
            )

        # Phase 1: Es gibt nur den FakeProvider. In Phase 4 wird hier der
        # gewünschte Anbieter (Stripe/PayPal) ausgewählt.
        provider = providers.get("fake")
        if provider is None:
            raise GameError("no_provider", "Aufladen ist noch nicht verfügbar", 503)
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
            raise GameError("unknown_provider", "Unbekannter Zahlungsanbieter", 404)
        return await process_webhook(provider, await request.body(), dict(request.headers))

    # ------------------------------------------------------------------ Login

    @app.post("/api/dev/login")
    async def api_dev_login(req: DevLoginRequest, response: Response):
        if not settings.dev_mode:
            raise GameError("not_found", "Nicht gefunden", 404)
        if not HANDLE_RE.match(req.handle):
            raise GameError("bad_handle", "Name: 2–24 Zeichen, nur Buchstaben, Ziffern, _ und -")
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

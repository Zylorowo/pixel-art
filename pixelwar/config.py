"""Konfiguration laden.

Die Werte stehen in `config.toml`. Hier werden sie einmal eingelesen, geprüft und
in ein unveränderliches `Settings`-Objekt gepackt. Der Rest des Codes bekommt
dieses Objekt übergeben, statt selbst Dateien oder Umgebungsvariablen zu lesen –
so lassen sich in Tests einfach andere Settings bauen.

Achtung: Die Preisparameter hier sind nur die STANDARDWERTE. Während des Events
werden sie im Admin-Panel geändert und in der Datenbank gespeichert (siehe
`pricing.PriceParams` und `game.Game.set_params`).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

from .pricing import PriceParams

DEFAULT_SECRET = "dev-only-change-me"
DEFAULT_ADMIN_PASSWORD = "admin"


@dataclass(frozen=True)
class Settings:
    event_start: datetime
    event_end: datetime
    palette: tuple[str, ...]
    db_path: str = "pixelwar.db"
    data_dir: str = "data"
    dev_mode: bool = False
    secret_key: str = DEFAULT_SECRET
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    timezone: str = "Europe/Berlin"
    night_pause: tuple[time, time] | None = None
    canvas_size: int = 400
    default_params: PriceParams = PriceParams()
    cooldown_interval_s: int = 60
    pixels_per_second: int = 5
    max_total_topup_cents: int = 2500
    topup_packages_cents: tuple[int, ...] = (300, 1000, 2500)
    guild_creation_cents: int = 500
    guild_image_max_bytes: int = 2 * 1024 * 1024
    flush_interval_s: float = 0.1
    client_queue_size: int = 100

    def __post_init__(self) -> None:
        # Früh und laut scheitern: Ein Tippfehler in der Config soll den Start
        # verhindern, nicht mitten im Event zu seltsamen Fehlern führen.
        if not 2 <= len(self.palette) <= 256:
            raise ValueError("Palette braucht 2–256 Einträge (Index passt in ein Byte)")
        if self.event_end <= self.event_start:
            raise ValueError("event.end muss nach event.start liegen")
        self.default_params.validate()
        if not self.dev_mode:
            if self.secret_key == DEFAULT_SECRET:
                raise ValueError("Ohne dev_mode muss PIXELWAR_SECRET gesetzt sein")
            if self.admin_password == DEFAULT_ADMIN_PASSWORD:
                raise ValueError("Ohne dev_mode muss PIXELWAR_ADMIN_PASSWORD gesetzt sein")


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Zeitpunkt ohne Zeitzone: {value!r} – bitte mit 'Z' (UTC) angeben")
    return dt.astimezone(timezone.utc)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("PIXELWAR_CONFIG", "config.toml"))
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    server = raw.get("server", {})
    event = raw["event"]
    canvas = raw.get("canvas", {})
    pricing = raw.get("pricing", {})
    limits = raw.get("limits", {})
    guilds = raw.get("guilds", {})
    realtime = raw.get("realtime", {})

    night_pause = None
    if "night_pause" in event:
        start, end = event["night_pause"]
        night_pause = (time.fromisoformat(start), time.fromisoformat(end))

    return Settings(
        dev_mode=bool(server.get("dev_mode", False)),
        db_path=server.get("db_path", "pixelwar.db"),
        data_dir=server.get("data_dir", "data"),
        # Geheimnisse bevorzugt aus Umgebungsvariablen, nicht aus der Datei.
        secret_key=os.environ.get("PIXELWAR_SECRET") or server.get("secret_key", DEFAULT_SECRET),
        admin_password=os.environ.get("PIXELWAR_ADMIN_PASSWORD")
        or server.get("admin_password", DEFAULT_ADMIN_PASSWORD),
        event_start=_parse_utc(event["start"]),
        event_end=_parse_utc(event["end"]),
        timezone=event.get("timezone", "Europe/Berlin"),
        night_pause=night_pause,
        canvas_size=int(canvas.get("size", 400)),
        default_params=PriceParams(
            base_cents=int(pricing.get("base_cents", 2)),
            factor=float(pricing.get("factor", 1.15)),
            contagion=float(pricing.get("contagion", 0.02)),
            cooldown_minutes=int(pricing.get("cooldown_minutes", 30)),
        ),
        cooldown_interval_s=int(pricing.get("cooldown_check_seconds", 60)),
        pixels_per_second=int(limits.get("pixels_per_second", 5)),
        max_total_topup_cents=int(limits.get("max_total_topup_cents", 2500)),
        topup_packages_cents=tuple(int(x) for x in limits.get("topup_packages_cents", (300, 1000, 2500))),
        guild_creation_cents=int(guilds.get("creation_cents", 500)),
        guild_image_max_bytes=int(guilds.get("image_max_kb", 2048)) * 1024,
        flush_interval_s=int(realtime.get("flush_interval_ms", 100)) / 1000,
        client_queue_size=int(realtime.get("client_queue_size", 100)),
        palette=tuple(raw["palette"]["colors"]),
    )

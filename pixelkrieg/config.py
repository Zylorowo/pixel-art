"""Konfiguration laden.

Die Werte stehen in `config.toml`. Hier werden sie einmal eingelesen, geprüft und
in ein unveränderliches `Settings`-Objekt gepackt. Der Rest des Codes bekommt
dieses Objekt übergeben, statt selbst Dateien oder Umgebungsvariablen zu lesen –
so lassen sich in Tests einfach andere Settings bauen.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

DEFAULT_SECRET = "dev-only-bitte-aendern"


@dataclass(frozen=True)
class Settings:
    event_start: datetime
    event_end: datetime
    palette: tuple[str, ...]
    db_path: str = "pixelkrieg.db"
    dev_mode: bool = False
    secret_key: str = DEFAULT_SECRET
    timezone: str = "Europe/Berlin"
    night_pause: tuple[time, time] | None = None
    canvas_size: int = 400
    zone_size: int = 20
    price_base_cents: int = 2
    # Der Faktor ist eine Kommazahl, aber das Ergebnis der Preisformel wird sofort
    # zu einer Ganzzahl in Cent gerundet. Mit Geld selbst wird nie in float gerechnet.
    price_factor: float = 1.15
    cooldown_after_s: int = 30 * 60
    cooldown_interval_s: int = 60
    pixels_per_second: int = 5
    max_total_topup_cents: int = 2500
    topup_packages_cents: tuple[int, ...] = (300, 1000, 2500)
    flush_interval_s: float = 0.1
    client_queue_size: int = 100

    def __post_init__(self) -> None:
        # Früh und laut scheitern: Ein Tippfehler in der Config soll den Start
        # verhindern, nicht mitten im Event zu seltsamen Fehlern führen.
        if self.canvas_size % self.zone_size:
            raise ValueError("canvas.size muss durch canvas.zone_size teilbar sein")
        if not 2 <= len(self.palette) <= 256:
            raise ValueError("Palette braucht 2–256 Einträge (Index passt in ein Byte)")
        if self.event_end <= self.event_start:
            raise ValueError("event.end muss nach event.start liegen")
        if not self.dev_mode and self.secret_key == DEFAULT_SECRET:
            raise ValueError("Ohne dev_mode muss PIXELKRIEG_SECRET gesetzt sein")

    @property
    def zones_per_side(self) -> int:
        return self.canvas_size // self.zone_size


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Zeitpunkt ohne Zeitzone: {value!r} – bitte mit 'Z' (UTC) angeben")
    return dt.astimezone(timezone.utc)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("PIXELKRIEG_CONFIG", "config.toml"))
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    server = raw.get("server", {})
    event = raw["event"]
    canvas = raw.get("canvas", {})
    pricing = raw.get("pricing", {})
    limits = raw.get("limits", {})
    realtime = raw.get("realtime", {})

    night_pause = None
    if "night_pause" in event:
        start, end = event["night_pause"]
        night_pause = (time.fromisoformat(start), time.fromisoformat(end))

    return Settings(
        dev_mode=bool(server.get("dev_mode", False)),
        db_path=server.get("db_path", "pixelkrieg.db"),
        secret_key=os.environ.get("PIXELKRIEG_SECRET") or server.get("secret_key", DEFAULT_SECRET),
        event_start=_parse_utc(event["start"]),
        event_end=_parse_utc(event["end"]),
        timezone=event.get("timezone", "Europe/Berlin"),
        night_pause=night_pause,
        canvas_size=int(canvas.get("size", 400)),
        zone_size=int(canvas.get("zone_size", 20)),
        price_base_cents=int(pricing.get("base_cents", 2)),
        price_factor=float(pricing.get("factor", 1.15)),
        cooldown_after_s=int(pricing.get("cooldown_after_minutes", 30)) * 60,
        cooldown_interval_s=int(pricing.get("cooldown_interval_seconds", 60)),
        pixels_per_second=int(limits.get("pixels_per_second", 5)),
        max_total_topup_cents=int(limits.get("max_total_topup_cents", 2500)),
        topup_packages_cents=tuple(int(x) for x in limits.get("topup_packages_cents", (300, 1000, 2500))),
        flush_interval_s=int(realtime.get("flush_interval_ms", 100)) / 1000,
        client_queue_size=int(realtime.get("client_queue_size", 100)),
        palette=tuple(raw["palette"]["colors"]),
    )

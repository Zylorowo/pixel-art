"""Event-Zeitraum: Läuft das Event gerade, und darf gekauft/gemalt werden?"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Settings

BEFORE = "before"
RUNNING = "running"
NIGHT_PAUSE = "night_pause"
ENDED = "ended"


def event_phase(settings: Settings, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now < settings.event_start:
        return BEFORE
    if now >= settings.event_end:
        return ENDED
    if settings.night_pause and _in_night_pause(settings, now):
        return NIGHT_PAUSE
    return RUNNING


def _in_night_pause(settings: Settings, now: datetime) -> bool:
    # Die Nachtpause ist in Ortszeit gemeint ("00:00–08:00 in Deutschland"),
    # gespeichert und verglichen wird aber alles in UTC. Deshalb hier umrechnen.
    local = now.astimezone(ZoneInfo(settings.timezone)).time()
    start, end = settings.night_pause
    if start <= end:
        return start <= local < end
    # Pause über Mitternacht, z. B. 23:00–07:00
    return local >= start or local < end

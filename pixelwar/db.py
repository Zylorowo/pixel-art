"""Datenbank: Verbindung, Schema, Transaktionen.

Die Datenbank ist die Wahrheit. Alles, was im Speicher liegt (NumPy-Array,
Heat-Werte), ist nur eine schnelle Kopie und wird beim Start von hier geladen.

Portabilität nach Postgres: Die Abfragen im Code benutzen nur Standard-SQL
(inkl. `INSERT ... ON CONFLICT`, das beide können). Die einzige Stelle, die bei
einer Migration angepasst werden muss, ist das Schema unten
(`INTEGER PRIMARY KEY` → `BIGINT GENERATED ALWAYS AS IDENTITY`) sowie die
Platzhalter `?` (psycopg benutzt `%s`).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    color_hex     TEXT NOT NULL,
    -- 0 = kein Bild. Wird bei jedem Hochladen erhöht, damit Browser nicht das
    -- alte Bild aus dem Cache zeigen (?v=<image_version> an der Bild-URL).
    image_version INTEGER NOT NULL DEFAULT 0,
    owner_user_id INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    handle        TEXT NOT NULL UNIQUE,
    email         TEXT UNIQUE,
    guild_id      INTEGER REFERENCES guilds(id),
    -- CHECK als letztes Sicherheitsnetz: Selbst wenn der Code einen Fehler hat,
    -- lehnt die Datenbank ein negatives Guthaben ab.
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    -- SMALLINT 0/1 statt BOOLEAN: funktioniert in SQLite und Postgres gleich.
    is_banned     SMALLINT NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- Folgen ist unabhängig von der Mitgliedschaft: Man kann vielen Gilden folgen,
-- aber nur in einer Mitglied sein.
CREATE TABLE IF NOT EXISTS guild_followers (
    guild_id      INTEGER NOT NULL REFERENCES guilds(id),
    user_id       INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Eine Zeile gibt es nur für Pixel, die schon bemalt wurden ODER durch
-- Ansteckung heat bekommen haben (dann color_index = 0, also noch leer).
CREATE TABLE IF NOT EXISTS pixels (
    x               INTEGER NOT NULL,
    y               INTEGER NOT NULL,
    color_index     INTEGER NOT NULL DEFAULT 0,
    owner_user_id   INTEGER,
    guild_id        INTEGER,
    -- heat ist eine Kommazahl (Ansteckung +0.02). Das ist KEIN Geld – der
    -- Preis daraus wird sofort zu ganzen Cent gerundet.
    heat            REAL NOT NULL DEFAULT 0,
    overpaint_count INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT,              -- letzte Bemalung (NULL = nie bemalt)
    cooled_at       TEXT NOT NULL,     -- Bezugszeitpunkt für die nächste Abkühlung
    PRIMARY KEY (x, y)
);
CREATE INDEX IF NOT EXISTS pixels_cooling ON pixels (cooled_at);

-- Im Admin-Panel geänderte Preisparameter. Fehlt ein Schlüssel, gilt der
-- Standardwert aus config.toml.
CREATE TABLE IF NOT EXISTS game_params (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);

-- Vollständiges Protokoll, wird nie gelöscht (Zeitraffer-Video am Ende).
CREATE TABLE IF NOT EXISTS paint_log (
    id            INTEGER PRIMARY KEY,
    x             INTEGER NOT NULL,
    y             INTEGER NOT NULL,
    color_index   INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    guild_id      INTEGER,
    price_cents   INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topups (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    provider      TEXT NOT NULL,
    provider_ref  TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
-- Idempotenz: Dieselbe Zahlung eines Anbieters kann nur EINMAL eingetragen
-- werden, egal wie oft der Webhook zugestellt wird.
CREATE UNIQUE INDEX IF NOT EXISTS topups_provider_ref ON topups (provider, provider_ref);
CREATE INDEX IF NOT EXISTS topups_user ON topups (user_id);

CREATE TABLE IF NOT EXISTS mod_events (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    x             INTEGER,
    y             INTEGER,
    detail        TEXT,
    resolved      SMALLINT NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
"""


def utc_iso(dt: datetime | None = None) -> str:
    """Zeitstempel als Text in UTC mit fester Länge.

    Feste Länge (immer Mikrosekunden, immer 'Z') ist wichtig: Dann kann man
    Zeitstempel in SQL einfach per `<` vergleichen, weil die Textsortierung der
    zeitlichen Reihenfolge entspricht.
    """
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: Python startet keine Transaktionen heimlich von selbst.
    # Wir schreiben BEGIN/COMMIT explizit (siehe `transaction`) – das ist
    # verständlicher und verhält sich wie in jeder anderen Datenbank.
    # check_same_thread=False: Die Schreib-Verbindung wird aus einem Worker-Thread
    # benutzt (siehe game.py). Ein Lock sorgt dafür, dass das nie gleichzeitig passiert.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL: Leser blockieren den Schreiber nicht und umgekehrt.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL ist im WAL-Modus sicher gegen Datenbank-Korruption; bei einem
    # Stromausfall können höchstens die letzten Millisekunden verloren gehen.
    # Dafür ist jeder Commit um ein Vielfaches schneller als mit FULL.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Alles im `with`-Block passiert ganz oder gar nicht.

    BEGIN IMMEDIATE holt sich sofort die Schreibsperre. Ohne IMMEDIATE würde
    SQLite erst beim ersten Schreiben sperren – dann könnten zwei Transaktionen
    dasselbe Guthaben lesen und beide abbuchen.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

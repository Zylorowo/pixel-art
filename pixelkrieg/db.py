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
CREATE TABLE IF NOT EXISTS clans (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    color_hex     TEXT NOT NULL,
    owner_user_id INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    handle        TEXT NOT NULL UNIQUE,
    email         TEXT UNIQUE,
    clan_id       INTEGER REFERENCES clans(id),
    -- CHECK als letztes Sicherheitsnetz: Selbst wenn der Code einen Fehler hat,
    -- lehnt die Datenbank ein negatives Guthaben ab.
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    -- SMALLINT 0/1 statt BOOLEAN: funktioniert in SQLite und Postgres gleich.
    is_banned     SMALLINT NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pixels (
    x             INTEGER NOT NULL,
    y             INTEGER NOT NULL,
    color_index   INTEGER NOT NULL,
    owner_user_id INTEGER,
    clan_id       INTEGER,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (x, y)
);

CREATE TABLE IF NOT EXISTS zones (
    zx            INTEGER NOT NULL,
    zy            INTEGER NOT NULL,
    heat          INTEGER NOT NULL DEFAULT 0,
    last_paint_at TEXT,
    -- Schon jetzt angelegt, damit Phase 3 (Moderation) keine Migration braucht.
    frozen        SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (zx, zy)
);

-- Vollständiges Protokoll, wird nie gelöscht (Zeitraffer-Video am Ende).
CREATE TABLE IF NOT EXISTS paint_log (
    id            INTEGER PRIMARY KEY,
    x             INTEGER NOT NULL,
    y             INTEGER NOT NULL,
    color_index   INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    clan_id       INTEGER,
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
    zx            INTEGER,
    zy            INTEGER,
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


def init_schema(conn: sqlite3.Connection, zones_per_side: int) -> None:
    conn.executescript(SCHEMA)
    # Alle Zonen einmal anlegen, damit später nur noch UPDATEs nötig sind.
    with transaction(conn):
        conn.executemany(
            "INSERT INTO zones (zx, zy, heat) VALUES (?, ?, 0) ON CONFLICT (zx, zy) DO NOTHING",
            [(zx, zy) for zy in range(zones_per_side) for zx in range(zones_per_side)],
        )


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

"""Der Spielkern: Leinwand, Pixelpreise, Malen, Abkühlung, Guthaben.

Aufbau in einem Satz: Jede Änderung läuft als EINE Datenbank-Transaktion, und
erst wenn die erfolgreich war, wird die Kopie im Speicher angepasst.

Warum ein Lock UND Transaktionen?
- Die Transaktion garantiert, dass Prüfung ("reicht das Guthaben?") und
  Abbuchung untrennbar zusammen passieren. Das verhindert das Ins-Minus-Malen
  per Doppelklick.
- Das `asyncio.Lock` sorgt dafür, dass immer nur EINE Schreiboperation zur Zeit
  läuft. SQLite kann ohnehin nur einen Schreiber gleichzeitig; mit dem Lock
  warten die anderen geordnet in Python, statt in SQLite-Timeouts zu laufen.
  Außerdem bleiben so Datenbank und Speicherkopie garantiert synchron.

Warum `asyncio.to_thread`?
FastAPI läuft in einer "Event-Loop": ein einziger Thread, der zwischen vielen
Anfragen hin- und herspringt, immer wenn eine auf etwas wartet (`await`).
SQLite-Aufrufe sind aber "blockierend" – während die Platte schreibt, stünde die
ganze Loop still, und keine WebSocket-Nachricht ginge raus. `to_thread` schiebt
die Datenbankarbeit in einen Hilfsthread; die Loop kann derweil weiterarbeiten.

Speicherkopie – vier Arrays à 400 × 400, Index immer [y, x]:
- canvas       Farbindex (0 = leer)
- heat         Kommazahl pro Pixel
- owner_guild  Gilden-ID des letzten Malers (0 = keine) → "gehaltene Pixel"
- prices       Preis in Cent, fertig ausgerechnet. Wird bei jeder heat-Änderung
               für die betroffenen Pixel neu berechnet, damit /api/prices und
               die Preisprüfung nie auseinanderlaufen.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np

from . import db
from .config import Settings
from .pricing import PriceParams, price_array, price_cents

NEIGHBORS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]


class GameError(Exception):
    """Ein erwarteter Fehler, der als verständliche Meldung beim Nutzer landet.

    `code` ist maschinenlesbar – das Frontend übersetzt ihn über die
    Sprachdatei (errors.<code>). `message` ist nur ein englischer Rückfall.
    """

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


@dataclass(frozen=True)
class PaintResult:
    x: int
    y: int
    color_index: int
    price_cents: int
    balance_cents: int
    # Alle Pixel (gemaltes + Nachbarn), deren Preis sich geändert hat: [(x, y, preis)]
    price_changes: list[tuple[int, int, int]]


@dataclass(frozen=True)
class TopupOutcome:
    status: str  # "paid", "duplicate", "refund_needed"
    balance_cents: int


class Game:
    def __init__(self, settings: Settings):
        self.settings = settings
        n = settings.canvas_size
        self.canvas = np.zeros((n, n), dtype=np.uint8)
        self.heat = np.zeros((n, n), dtype=np.float64)
        self.owner_guild = np.zeros((n, n), dtype=np.int32)
        self.prices = np.zeros((n, n), dtype=np.int64)
        self.params: PriceParams = settings.default_params
        self._lock = asyncio.Lock()
        # Zwei Verbindungen: Eine zum Schreiben (nur unter dem Lock benutzt) und
        # eine zum Lesen. Dank WAL-Modus stören sie sich nicht gegenseitig.
        self._writer: sqlite3.Connection | None = None
        self._reader: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ Start/Stopp

    def open(self) -> None:
        self._writer = db.connect(self.settings.db_path)
        self._reader = db.connect(self.settings.db_path)
        db.init_schema(self._writer)
        self._load()

    def close(self) -> None:
        for conn in (self._writer, self._reader):
            if conn is not None:
                conn.close()

    @property
    def reader(self) -> sqlite3.Connection:
        return self._reader

    def _load(self) -> None:
        """Speicherkopie aus der Datenbank aufbauen (die DB ist die Wahrheit)."""
        self.canvas[:] = 0
        self.heat[:] = 0
        self.owner_guild[:] = 0
        for r in self._reader.execute("SELECT x, y, color_index, heat, guild_id FROM pixels"):
            self.canvas[r["y"], r["x"]] = r["color_index"]
            self.heat[r["y"], r["x"]] = r["heat"]
            self.owner_guild[r["y"], r["x"]] = r["guild_id"] or 0
        self.params = self._load_params()
        self._refresh_all_prices()

    def _load_params(self) -> PriceParams:
        stored = {r["key"]: r["value"] for r in self._reader.execute("SELECT key, value FROM game_params")}
        d = self.settings.default_params
        p = PriceParams(
            base_cents=int(stored.get("base_cents", d.base_cents)),
            factor=float(stored.get("factor", d.factor)),
            contagion=float(stored.get("contagion", d.contagion)),
            cooldown_minutes=int(stored.get("cooldown_minutes", d.cooldown_minutes)),
        )
        p.validate()
        return p

    # ------------------------------------------------------------------ Hilfen

    async def write(self, fn: Callable[..., Any], *args) -> Any:
        """Eine Schreibfunktion `fn(conn, *args)` unter dem Lock im Hilfsthread ausführen.

        Für Aktionen außerhalb des Spielkerns (z. B. Gilden, siehe guilds.py),
        die dieselbe Schreib-Verbindung brauchen.
        """
        async with self._lock:
            return await asyncio.to_thread(fn, self._writer, *args)

    def _refresh_all_prices(self) -> None:
        self.prices[:] = price_array(self.heat, self.canvas == 0, self.params)

    def _refresh_prices(self, points: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
        """Preise für einzelne Pixel neu berechnen; zurück kommen nur echte Änderungen."""
        if not points:
            return []
        xs = np.fromiter((p[0] for p in points), dtype=np.intp, count=len(points))
        ys = np.fromiter((p[1] for p in points), dtype=np.intp, count=len(points))
        new = price_array(self.heat[ys, xs], self.canvas[ys, xs] == 0, self.params)
        changed = new != self.prices[ys, xs]
        self.prices[ys, xs] = new
        return [(int(x), int(y), int(p)) for x, y, p in zip(xs[changed], ys[changed], new[changed])]

    def held_pixels(self, guild_id: int) -> int:
        return int(np.count_nonzero(self.owner_guild == guild_id))

    # ------------------------------------------------------------------ Malen

    def validate_paint(self, x: int, y: int, color_index: int) -> None:
        n = self.settings.canvas_size
        if not (0 <= x < n and 0 <= y < n):
            raise GameError("out_of_bounds", "Pixel is outside the canvas")
        # Index 0 bedeutet "leer" und ist nicht malbar – sonst könnte man ein
        # Pixel "leeren", und der Nächste würde es zum Grundpreis übermalen.
        if not (1 <= color_index < len(self.settings.palette)):
            raise GameError("bad_color", "Unknown color")

    async def paint(
        self,
        user_id: int,
        x: int,
        y: int,
        color_index: int,
        max_price_cents: int | None = None,
        now: datetime | None = None,
    ) -> PaintResult:
        self.validate_paint(x, y, color_index)
        now = now or datetime.now(timezone.utc)
        async with self._lock:
            params = self.params
            tx = await asyncio.to_thread(
                self._paint_tx, user_id, x, y, color_index, max_price_cents, now, params
            )
            # Erst NACH erfolgreichem Commit die Speicherkopie anpassen. Schlägt
            # die Transaktion fehl, kommen wir hier gar nicht hin.
            self.canvas[y, x] = color_index
            self.heat[y, x] = tx["heat"]
            self.owner_guild[y, x] = tx["guild_id"] or 0
            for nx, ny in tx["neighbors"]:
                self.heat[ny, nx] += params.contagion
            changes = self._refresh_prices([(x, y), *tx["neighbors"]])
        return PaintResult(
            x=x, y=y, color_index=color_index, price_cents=tx["price"],
            balance_cents=tx["balance"], price_changes=changes,
        )

    def _paint_tx(self, user_id, x, y, color_index, max_price_cents, now, params: PriceParams) -> dict:
        conn = self._writer
        n = self.settings.canvas_size
        ts = db.utc_iso(now)
        with db.transaction(conn):
            old = conn.execute(
                "SELECT color_index, owner_user_id, heat, overpaint_count FROM pixels WHERE x = ? AND y = ?",
                (x, y),
            ).fetchone()
            empty = old is None or old["color_index"] == 0
            heat = old["heat"] if old else 0.0
            price = price_cents(heat, empty, params)

            # Das Frontend schickt den Preis mit, den es angezeigt hat. Ist der
            # Preis inzwischen gestiegen (jemand anderes war schneller), buchen
            # wir NICHT still den höheren Preis ab, sondern fragen nach.
            if max_price_cents is not None and price > max_price_cents:
                raise GameError("price_changed", "The price just went up", 409, current_price_cents=price)

            user = conn.execute("SELECT guild_id, is_banned FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise GameError("not_logged_in", "Unknown account", 401)
            if user["is_banned"]:
                raise GameError("banned", "This account is banned", 403)
            if not empty and old["color_index"] == color_index and old["owner_user_id"] == user_id:
                # Schutz vor Doppelklick: Nochmal dasselbe Pixel in derselben Farbe
                # zu kaufen ändert nichts, kostet aber Geld und treibt den Preis hoch.
                raise GameError("no_change", "This pixel already has that color", 409)

            # Prüfen und Abbuchen in EINER Anweisung: "ziehe ab, aber nur wenn genug da ist".
            # Die Datenbank führt das atomar aus. rowcount sagt uns, ob es geklappt hat.
            cur = conn.execute(
                "UPDATE users SET balance_cents = balance_cents - ? WHERE id = ? AND balance_cents >= ?",
                (price, user_id, price),
            )
            if cur.rowcount != 1:
                raise GameError("insufficient_funds", "Not enough credit", 402, price_cents=price)

            # Leeres Pixel füllen: heat bleibt, wie er ist (kann durch Ansteckung
            # schon > 0 sein – das merkt man erst beim nächsten Übermalen).
            # Übermalen: heat + 1, und die Nachbarn werden angesteckt.
            count = old["overpaint_count"] if old else 0
            new_heat = heat if empty else heat + 1
            if not empty:
                count += 1
            conn.execute(
                "INSERT INTO pixels (x, y, color_index, owner_user_id, guild_id, heat, overpaint_count, updated_at, cooled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (x, y) DO UPDATE SET color_index = excluded.color_index, "
                "owner_user_id = excluded.owner_user_id, guild_id = excluded.guild_id, "
                "heat = excluded.heat, overpaint_count = excluded.overpaint_count, "
                "updated_at = excluded.updated_at, cooled_at = excluded.cooled_at",
                (x, y, color_index, user_id, user["guild_id"], new_heat, count, ts, ts),
            )

            neighbors: list[tuple[int, int]] = []
            if not empty and params.contagion > 0:
                for dx, dy in NEIGHBORS:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < n and 0 <= ny < n:
                        neighbors.append((nx, ny))
                # Existiert der Nachbar noch nicht in der Tabelle, wird er als leeres
                # Pixel mit etwas heat angelegt; sonst wird sein heat erhöht.
                # Die Abkühl-Uhr eines bestehenden Nachbarn läuft weiter – Ansteckung
                # ist keine Bemalung.
                conn.executemany(
                    "INSERT INTO pixels (x, y, color_index, heat, cooled_at) VALUES (?, ?, 0, ?, ?) "
                    "ON CONFLICT (x, y) DO UPDATE SET heat = pixels.heat + excluded.heat",
                    [(nx, ny, params.contagion, ts) for nx, ny in neighbors],
                )

            conn.execute(
                "INSERT INTO paint_log (x, y, color_index, user_id, guild_id, price_cents, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (x, y, color_index, user_id, user["guild_id"], price, ts),
            )
            balance = conn.execute("SELECT balance_cents FROM users WHERE id = ?", (user_id,)).fetchone()[0]

        return {"price": price, "balance": balance, "heat": new_heat,
                "guild_id": user["guild_id"], "neighbors": neighbors}

    # ------------------------------------------------------------------ Abkühlung

    async def cool_down(self, now: datetime | None = None) -> list[tuple[int, int, int]]:
        """Pro `cooldown_minutes` ohne Bemalung verliert ein Pixel 1 heat (nie unter 0).

        Läuft jede Minute und erwischt die Pixel, deren Abkühl-Uhr abgelaufen ist.
        Die Uhr wird dann neu gestellt – beim nächsten Mal sind sie erst nach
        weiteren `cooldown_minutes` wieder dran. Ohne Abkühlung würden umkämpfte
        Stellen irgendwann so teuer, dass niemand mehr malt.

        Zurück kommen die Pixel, deren PREIS sich dadurch geändert hat.
        """
        now = now or datetime.now(timezone.utc)
        async with self._lock:
            cutoff = db.utc_iso(now - timedelta(minutes=self.params.cooldown_minutes))
            cooled = await asyncio.to_thread(self._cool_down_tx, cutoff, db.utc_iso(now))
            for x, y, h in cooled:
                self.heat[y, x] = h
            return self._refresh_prices([(x, y) for x, y, _ in cooled])

    def _cool_down_tx(self, cutoff: str, now_ts: str) -> list[tuple[int, int, float]]:
        conn = self._writer
        with db.transaction(conn):
            rows = conn.execute(
                "SELECT x, y, heat FROM pixels WHERE heat > 0 AND cooled_at <= ?", (cutoff,)
            ).fetchall()
            # CASE statt MAX(): MAX mit zwei Argumenten heißt in Postgres GREATEST.
            conn.execute(
                "UPDATE pixels SET heat = CASE WHEN heat > 1 THEN heat - 1 ELSE 0 END, cooled_at = ? "
                "WHERE heat > 0 AND cooled_at <= ?",
                (now_ts, cutoff),
            )
            # Aufräumen: leere Pixel ohne heat brauchen keine Zeile mehr.
            conn.execute("DELETE FROM pixels WHERE color_index = 0 AND heat = 0")
        return [(r["x"], r["y"], max(0.0, r["heat"] - 1)) for r in rows]

    # ------------------------------------------------------------------ Parameter

    async def set_params(self, params: PriceParams) -> PriceParams:
        """Preisparameter zur Laufzeit ändern (Admin-Panel). Wirkt sofort auf alle Pixel."""
        params.validate()
        async with self._lock:
            await asyncio.to_thread(self._set_params_tx, params)
            self.params = params
            self._refresh_all_prices()
        return params

    def _set_params_tx(self, params: PriceParams) -> None:
        conn = self._writer
        with db.transaction(conn):
            conn.executemany(
                "INSERT INTO game_params (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                [(k, str(v)) for k, v in params.as_dict().items()],
            )

    # ------------------------------------------------------------------ Pixel-Info

    def pixel_info(self, x: int, y: int) -> dict:
        row = self._reader.execute(
            "SELECT p.color_index, p.overpaint_count, p.updated_at, u.handle, "
            "g.slug AS guild_slug, g.name AS guild_name, g.color_hex AS guild_color "
            "FROM pixels p LEFT JOIN users u ON u.id = p.owner_user_id "
            "LEFT JOIN guilds g ON g.id = p.guild_id WHERE p.x = ? AND p.y = ?",
            (x, y),
        ).fetchone()
        info = {
            "x": x, "y": y,
            "color_index": int(self.canvas[y, x]),
            "price_cents": int(self.prices[y, x]),
            "overpaint_count": 0, "painted_at": None, "user": None, "guild": None,
        }
        if row and row["color_index"]:
            info["overpaint_count"] = row["overpaint_count"]
            info["painted_at"] = row["updated_at"]
            info["user"] = row["handle"]
            if row["guild_slug"]:
                info["guild"] = {"slug": row["guild_slug"], "name": row["guild_name"], "color": row["guild_color"]}
        return info

    # ------------------------------------------------------------------ Accounts

    async def get_or_create_user(self, handle: str) -> int:
        return await self.write(_get_or_create_user_tx, handle)

    def get_user(self, user_id: int) -> dict | None:
        row = self._reader.execute(
            "SELECT id, handle, guild_id, balance_cents, is_banned FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def total_topup_cents(self, user_id: int) -> int:
        return self._reader.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM topups WHERE user_id = ? AND status = 'paid'",
            (user_id,),
        ).fetchone()[0]

    # ------------------------------------------------------------------ Guthaben

    async def credit_topup(self, provider: str, provider_ref: str, user_id: int, amount_cents: int) -> TopupOutcome:
        """Eine bestätigte Zahlung gutschreiben – idempotent.

        Zahlungsanbieter stellen Webhooks gern doppelt zu. Deshalb: Gibt es die
        Kombination (provider, provider_ref) schon, passiert nichts.
        """
        return await self.write(
            _credit_topup_tx, provider, provider_ref, user_id, amount_cents, self.settings.max_total_topup_cents
        )


def _get_or_create_user_tx(conn: sqlite3.Connection, handle: str) -> int:
    with db.transaction(conn):
        row = conn.execute("SELECT id FROM users WHERE handle = ?", (handle,)).fetchone()
        if row:
            return row["id"]
        return conn.execute(
            "INSERT INTO users (handle, created_at) VALUES (?, ?)", (handle, db.utc_iso())
        ).lastrowid


def _credit_topup_tx(conn, provider, provider_ref, user_id, amount_cents, cap) -> TopupOutcome:
    try:
        with db.transaction(conn):
            seen = conn.execute(
                "SELECT 1 FROM topups WHERE provider = ? AND provider_ref = ?", (provider, provider_ref)
            ).fetchone()
            if seen:
                status = "duplicate"
            else:
                total = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM topups WHERE user_id = ? AND status = 'paid'",
                    (user_id,),
                ).fetchone()[0]
                # Die Obergrenze wird schon vor dem Bezahlen geprüft. Hier nochmal,
                # falls jemand zwei Bezahlfenster gleichzeitig geöffnet hat. Das
                # Geld ist dann schon bezahlt – wir schreiben es nicht gut, sondern
                # markieren es zur Rückerstattung.
                status = "paid" if total + amount_cents <= cap else "refund_needed"
                conn.execute(
                    "INSERT INTO topups (user_id, amount_cents, provider, provider_ref, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, amount_cents, provider, provider_ref, status, db.utc_iso()),
                )
                if status == "paid":
                    conn.execute(
                        "UPDATE users SET balance_cents = balance_cents + ? WHERE id = ?", (amount_cents, user_id)
                    )
    except sqlite3.IntegrityError:
        # Der Unique-Index hat zugeschlagen – die harte Garantie hinter der
        # freundlichen Prüfung oben.
        status = "duplicate"
    balance = conn.execute("SELECT balance_cents FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    return TopupOutcome(status=status, balance_cents=balance)

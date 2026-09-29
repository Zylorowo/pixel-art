"""Der Spielkern: Leinwand, Zonen, Malen, Abkühlung, Guthaben.

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
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db
from .config import Settings
from .pricing import price_cents


class GameError(Exception):
    """Ein erwarteter Fehler, der als verständliche Meldung beim Nutzer landet."""

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
    zx: int
    zy: int
    heat: int
    zone_price_cents: int


@dataclass(frozen=True)
class ZoneChange:
    zx: int
    zy: int
    heat: int
    price_cents: int


@dataclass(frozen=True)
class TopupOutcome:
    status: str  # "paid", "duplicate", "refund_needed"
    balance_cents: int


class Game:
    def __init__(self, settings: Settings):
        self.settings = settings
        n = settings.canvas_size
        nz = settings.zones_per_side
        # Indexreihenfolge [y, x] (Zeile, Spalte), wie bei Bildern üblich. Dann
        # ist `canvas.tobytes()` direkt Zeile für Zeile – genau das Format, das
        # das Frontend erwartet.
        self.canvas = np.zeros((n, n), dtype=np.uint8)
        self.heat = np.zeros((nz, nz), dtype=np.int32)  # heat[zy, zx]
        self.frozen = np.zeros((nz, nz), dtype=bool)
        self._lock = asyncio.Lock()
        # Zwei Verbindungen: Eine zum Schreiben (nur unter dem Lock benutzt) und
        # eine zum Lesen. Dank WAL-Modus stören sie sich nicht gegenseitig.
        self._writer: sqlite3.Connection | None = None
        self._reader: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ Start/Stopp

    def open(self) -> None:
        self._writer = db.connect(self.settings.db_path)
        self._reader = db.connect(self.settings.db_path)
        db.init_schema(self._writer, self.settings.zones_per_side)
        self._load()

    def close(self) -> None:
        for conn in (self._writer, self._reader):
            if conn is not None:
                conn.close()

    def _load(self) -> None:
        """Speicherkopie aus der Datenbank aufbauen (die DB ist die Wahrheit)."""
        self.canvas[:] = 0
        for row in self._reader.execute("SELECT x, y, color_index FROM pixels"):
            self.canvas[row["y"], row["x"]] = row["color_index"]
        for row in self._reader.execute("SELECT zx, zy, heat, frozen FROM zones"):
            self.heat[row["zy"], row["zx"]] = row["heat"]
            self.frozen[row["zy"], row["zx"]] = bool(row["frozen"])

    # ------------------------------------------------------------------ Preise

    def price_for_heat(self, heat: int) -> int:
        return price_cents(int(heat), self.settings.price_base_cents, self.settings.price_factor)

    def zone_price(self, zx: int, zy: int) -> int:
        return self.price_for_heat(self.heat[zy, zx])

    def zone_of(self, x: int, y: int) -> tuple[int, int]:
        return x // self.settings.zone_size, y // self.settings.zone_size

    # ------------------------------------------------------------------ Malen

    def validate_paint(self, x: int, y: int, color_index: int) -> None:
        n = self.settings.canvas_size
        if not (0 <= x < n and 0 <= y < n):
            raise GameError("out_of_bounds", "Pixel liegt außerhalb der Leinwand")
        # Index 0 bedeutet "leer" und ist nicht malbar – sonst könnte man ein
        # Pixel "leeren" und der Nächste würde es ohne Heat-Anstieg übermalen.
        if not (1 <= color_index < len(self.settings.palette)):
            raise GameError("bad_color", "Unbekannte Farbe")

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
            result = await asyncio.to_thread(
                self._paint_tx, user_id, x, y, color_index, max_price_cents, now
            )
            # Erst NACH erfolgreichem Commit die Speicherkopie anpassen. Schlägt
            # die Transaktion fehl, kommen wir hier gar nicht hin.
            self.canvas[y, x] = color_index
            self.heat[result.zy, result.zx] = result.heat
        return result

    def _paint_tx(self, user_id, x, y, color_index, max_price_cents, now) -> PaintResult:
        conn = self._writer
        zx, zy = self.zone_of(x, y)
        ts = db.utc_iso(now)
        with db.transaction(conn):
            zone = conn.execute(
                "SELECT heat, frozen FROM zones WHERE zx = ? AND zy = ?", (zx, zy)
            ).fetchone()
            if zone["frozen"]:
                raise GameError("zone_frozen", "Diese Zone ist gerade gesperrt", 423)

            heat = zone["heat"]
            price = self.price_for_heat(heat)
            # Das Frontend schickt den Preis mit, den es angezeigt hat. Ist der
            # Preis inzwischen gestiegen (jemand anderes war schneller), buchen
            # wir NICHT still den höheren Preis ab, sondern fragen nach.
            if max_price_cents is not None and price > max_price_cents:
                raise GameError(
                    "price_changed", "Der Preis ist gerade gestiegen", 409, current_price_cents=price
                )

            user = conn.execute(
                "SELECT clan_id, is_banned FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise GameError("unknown_user", "Unbekannter Account", 401)
            if user["is_banned"]:
                raise GameError("banned", "Dieser Account ist gesperrt", 403)

            old = conn.execute(
                "SELECT color_index, owner_user_id FROM pixels WHERE x = ? AND y = ?", (x, y)
            ).fetchone()
            if old is not None and old["color_index"] == color_index and old["owner_user_id"] == user_id:
                # Schutz vor Doppelklick: Nochmal dasselbe Pixel in derselben Farbe
                # zu kaufen ändert nichts, kostet aber Geld und treibt den Preis hoch.
                raise GameError("no_change", "Das Pixel hat schon diese Farbe", 409)

            # Prüfen und Abbuchen in EINER Anweisung: "ziehe ab, aber nur wenn genug da ist".
            # Die Datenbank führt das atomar aus. rowcount sagt uns, ob es geklappt hat.
            cur = conn.execute(
                "UPDATE users SET balance_cents = balance_cents - ? "
                "WHERE id = ? AND balance_cents >= ?",
                (price, user_id, price),
            )
            if cur.rowcount != 1:
                raise GameError(
                    "insufficient_funds", "Nicht genug Guthaben", 402, price_cents=price
                )

            # Übermalen (Pixel hatte schon eine Farbe) macht die Zone heißer.
            # Ein leeres Pixel füllen nicht – die leere Leinwand soll billig bleiben.
            overpaint = old is not None and old["color_index"] != 0
            new_heat = heat + 1 if overpaint else heat
            conn.execute(
                "UPDATE zones SET heat = ?, last_paint_at = ? WHERE zx = ? AND zy = ?",
                (new_heat, ts, zx, zy),
            )
            conn.execute(
                "INSERT INTO pixels (x, y, color_index, owner_user_id, clan_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (x, y) DO UPDATE SET color_index = excluded.color_index, "
                "owner_user_id = excluded.owner_user_id, clan_id = excluded.clan_id, "
                "updated_at = excluded.updated_at",
                (x, y, color_index, user_id, user["clan_id"], ts),
            )
            conn.execute(
                "INSERT INTO paint_log (x, y, color_index, user_id, clan_id, price_cents, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (x, y, color_index, user_id, user["clan_id"], price, ts),
            )
            balance = conn.execute(
                "SELECT balance_cents FROM users WHERE id = ?", (user_id,)
            ).fetchone()["balance_cents"]

        return PaintResult(
            x=x, y=y, color_index=color_index, price_cents=price, balance_cents=balance,
            zx=zx, zy=zy, heat=new_heat, zone_price_cents=self.price_for_heat(new_heat),
        )

    # ------------------------------------------------------------------ Abkühlung

    async def cool_down(self, now: datetime | None = None) -> list[ZoneChange]:
        """Jede Zone, in der seit `cooldown_after` nichts gemalt wurde, verliert 1 Heat.

        Läuft jede Minute. Ohne Abkühlung würden umkämpfte Zonen irgendwann so
        teuer, dass niemand mehr malt – das Event friert ein.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = db.utc_iso(now - timedelta(seconds=self.settings.cooldown_after_s))
        async with self._lock:
            changes = await asyncio.to_thread(self._cool_down_tx, cutoff)
            for c in changes:
                self.heat[c.zy, c.zx] = c.heat
        return changes

    def _cool_down_tx(self, cutoff: str) -> list[ZoneChange]:
        conn = self._writer
        with db.transaction(conn):
            rows = conn.execute(
                "SELECT zx, zy, heat FROM zones WHERE heat > 0 AND last_paint_at < ?", (cutoff,)
            ).fetchall()
            conn.execute(
                "UPDATE zones SET heat = heat - 1 WHERE heat > 0 AND last_paint_at < ?", (cutoff,)
            )
        return [
            ZoneChange(r["zx"], r["zy"], r["heat"] - 1, self.price_for_heat(r["heat"] - 1))
            for r in rows
        ]

    # ------------------------------------------------------------------ Accounts

    async def get_or_create_user(self, handle: str) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._get_or_create_user_tx, handle)

    def _get_or_create_user_tx(self, handle: str) -> int:
        conn = self._writer
        with db.transaction(conn):
            row = conn.execute("SELECT id FROM users WHERE handle = ?", (handle,)).fetchone()
            if row:
                return row["id"]
            cur = conn.execute(
                "INSERT INTO users (handle, created_at) VALUES (?, ?)", (handle, db.utc_iso())
            )
            return cur.lastrowid

    def get_user(self, user_id: int) -> dict | None:
        row = self._reader.execute(
            "SELECT id, handle, clan_id, balance_cents, is_banned FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def total_topup_cents(self, user_id: int) -> int:
        return self._reader.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM topups WHERE user_id = ? AND status = 'paid'",
            (user_id,),
        ).fetchone()["s"]

    # ------------------------------------------------------------------ Guthaben

    async def credit_topup(self, provider: str, provider_ref: str, user_id: int, amount_cents: int) -> TopupOutcome:
        """Eine bestätigte Zahlung gutschreiben – idempotent.

        Zahlungsanbieter stellen Webhooks gern doppelt zu. Deshalb: Gibt es die
        Kombination (provider, provider_ref) schon, passiert nichts.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._credit_topup_tx, provider, provider_ref, user_id, amount_cents
            )

    def _credit_topup_tx(self, provider, provider_ref, user_id, amount_cents) -> TopupOutcome:
        conn = self._writer
        cap = self.settings.max_total_topup_cents
        try:
            with db.transaction(conn):
                seen = conn.execute(
                    "SELECT 1 FROM topups WHERE provider = ? AND provider_ref = ?",
                    (provider, provider_ref),
                ).fetchone()
                if seen:
                    status = "duplicate"
                else:
                    total = conn.execute(
                        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM topups "
                        "WHERE user_id = ? AND status = 'paid'",
                        (user_id,),
                    ).fetchone()["s"]
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
                            "UPDATE users SET balance_cents = balance_cents + ? WHERE id = ?",
                            (amount_cents, user_id),
                        )
        except sqlite3.IntegrityError:
            # Der Unique-Index hat zugeschlagen – die harte Garantie hinter der
            # freundlichen Prüfung oben.
            status = "duplicate"
        balance = conn.execute(
            "SELECT balance_cents FROM users WHERE id = ?", (user_id,)
        ).fetchone()["balance_cents"]
        return TopupOutcome(status=status, balance_cents=balance)

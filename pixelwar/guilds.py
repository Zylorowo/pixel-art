"""Gilden: gründen, beitreten, folgen, Gildenseite.

Gilden funktionieren wie Kanäle: Man kann vielen folgen, ist aber höchstens in
EINER Mitglied – und das dauerhaft (kein Wechsel, sonst laufen kurz vor Schluss
alle zum Gewinner über).

Die Schreibfunktionen hier (`*_tx`) bekommen die Schreib-Verbindung übergeben
und werden über `Game.write` aufgerufen – also unter demselben Lock wie das
Malen. Das ist wichtig, weil das Gründen Guthaben abbucht.
"""

from __future__ import annotations

import io
import re
import sqlite3
from pathlib import Path

from . import db
from .game import GameError

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,22}[a-z0-9])$")  # 3–24 Zeichen
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
RESERVED_SLUGS = {"admin", "api", "new", "static", "overlay", "guilds"}
IMAGE_SIZE = 256


def validate_guild_fields(slug: str | None, name: str | None, description: str | None, color: str | None) -> None:
    if slug is not None and (not SLUG_RE.match(slug) or slug in RESERVED_SLUGS):
        raise GameError("bad_slug", "Link name: 3–24 characters, a–z, 0–9 and -")
    if name is not None and not 3 <= len(name.strip()) <= 32:
        raise GameError("bad_guild_name", "Name: 3–32 characters")
    if description is not None and len(description) > 500:
        raise GameError("bad_description", "Description: at most 500 characters")
    if color is not None and not COLOR_RE.match(color):
        raise GameError("bad_color_hex", "Invalid color")


# ---------------------------------------------------------------------- Schreiben


def create_guild_tx(conn: sqlite3.Connection, user_id: int, slug: str, name: str,
                    description: str, color: str, cost_cents: int) -> int:
    with db.transaction(conn):
        user = conn.execute("SELECT guild_id, is_banned FROM users WHERE id = ?", (user_id,)).fetchone()
        if user["is_banned"]:
            raise GameError("banned", "This account is banned", 403)
        if user["guild_id"] is not None:
            raise GameError("already_in_guild", "You are already in a guild", 409)
        if conn.execute("SELECT 1 FROM guilds WHERE slug = ?", (slug,)).fetchone():
            raise GameError("slug_taken", "This link name is taken", 409)
        # Gründungsgebühr: gleiches Muster wie beim Malen – prüfen und abbuchen in
        # einer Anweisung, damit man sich nicht per Doppelklick ins Minus gründet.
        cur = conn.execute(
            "UPDATE users SET balance_cents = balance_cents - ? WHERE id = ? AND balance_cents >= ?",
            (cost_cents, user_id, cost_cents),
        )
        if cur.rowcount != 1:
            raise GameError("insufficient_funds", "Not enough credit", 402, price_cents=cost_cents)
        guild_id = conn.execute(
            "INSERT INTO guilds (slug, name, description, color_hex, owner_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, name.strip(), description.strip(), color.upper(), user_id, db.utc_iso()),
        ).lastrowid
        conn.execute("UPDATE users SET guild_id = ? WHERE id = ?", (guild_id, user_id))
        return guild_id


def join_guild_tx(conn: sqlite3.Connection, user_id: int, guild_id: int) -> None:
    with db.transaction(conn):
        # "WHERE guild_id IS NULL" erzwingt das Wechselverbot direkt in der Datenbank.
        cur = conn.execute(
            "UPDATE users SET guild_id = ? WHERE id = ? AND guild_id IS NULL", (guild_id, user_id)
        )
        if cur.rowcount == 1:
            return
        current = conn.execute("SELECT guild_id FROM users WHERE id = ?", (user_id,)).fetchone()["guild_id"]
        if current != guild_id:
            raise GameError("already_in_guild", "You are already in a guild – switching is not allowed", 409)
        # Schon Mitglied genau dieser Gilde: nichts zu tun (zweimal klicken ist harmlos).


def follow_tx(conn: sqlite3.Connection, user_id: int, guild_id: int, follow: bool) -> None:
    with db.transaction(conn):
        if follow:
            conn.execute(
                "INSERT INTO guild_followers (guild_id, user_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                (guild_id, user_id, db.utc_iso()),
            )
        else:
            conn.execute("DELETE FROM guild_followers WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))


def update_guild_tx(conn: sqlite3.Connection, guild_id: int, description: str | None, color: str | None) -> None:
    with db.transaction(conn):
        if description is not None:
            conn.execute("UPDATE guilds SET description = ? WHERE id = ?", (description.strip(), guild_id))
        if color is not None:
            conn.execute("UPDATE guilds SET color_hex = ? WHERE id = ?", (color.upper(), guild_id))


def bump_image_version_tx(conn: sqlite3.Connection, guild_id: int) -> int:
    with db.transaction(conn):
        conn.execute("UPDATE guilds SET image_version = image_version + 1 WHERE id = ?", (guild_id,))
        return conn.execute("SELECT image_version FROM guilds WHERE id = ?", (guild_id,)).fetchone()[0]


# ---------------------------------------------------------------------- Lesen


def get_guild(conn: sqlite3.Connection, slug: str) -> dict:
    row = conn.execute(
        "SELECT g.*, u.handle AS owner_handle FROM guilds g JOIN users u ON u.id = g.owner_user_id "
        "WHERE g.slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        raise GameError("guild_not_found", "Guild not found", 404)
    return dict(row)


def guild_counts(conn: sqlite3.Connection, guild_id: int) -> tuple[int, int]:
    members = conn.execute("SELECT COUNT(*) FROM users WHERE guild_id = ?", (guild_id,)).fetchone()[0]
    followers = conn.execute("SELECT COUNT(*) FROM guild_followers WHERE guild_id = ?", (guild_id,)).fetchone()[0]
    return members, followers


def is_following(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM guild_followers WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone() is not None


def list_guilds(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT g.id, g.slug, g.name, g.color_hex, g.image_version, "
        "(SELECT COUNT(*) FROM users u WHERE u.guild_id = g.id) AS members, "
        "(SELECT COUNT(*) FROM guild_followers f WHERE f.guild_id = g.id) AS followers "
        "FROM guilds g"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- Bild


def image_path(data_dir: str, guild_id: int) -> Path:
    return Path(data_dir) / "guild_images" / f"{guild_id}.png"


def process_image(raw: bytes, dest: Path) -> None:
    """Hochgeladenes Bild prüfen, auf 256×256 zuschneiden und als PNG NEU speichern.

    Warum neu speichern statt die Datei so abzulegen? Beim Neu-Kodieren bleiben nur
    die Bildpunkte übrig. Versteckte Metadaten (GPS-Position im Handyfoto!) oder
    Dateien, die sich nur als Bild ausgeben, fallen dabei weg.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(raw))
        # "Dekompressionsbombe": winzige Datei, die entpackt Gigabytes belegt.
        if img.width * img.height > 40_000_000:
            raise GameError("bad_image", "Image is too large")
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise GameError("bad_image", "Not a valid image (PNG, JPEG, GIF or WebP)")

    img = img.convert("RGBA")
    # Mittig quadratisch zuschneiden, dann verkleinern.
    side = min(img.width, img.height)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    img.save(tmp, "PNG")
    tmp.replace(dest)  # erst am Ende austauschen: nie ein halb geschriebenes Bild ausliefern

# Pixel War

Bezahlte Pixel-Leinwand als zeitlich begrenztes Event. Domain: **pixelwar.gg**.
Spezifikation: [`SPEC.md`](SPEC.md).

**Stand:** Der Spielkern (Phase 1) ist fertig, aus Phase 2 ist der Gilden-Teil vorgezogen:
- Preise pro Pixel mit Nachbar-Ansteckung und Abkühlung
- Admin-Panel für die Preisparameter, änderbar zur Laufzeit ohne Neustart
- Live-Verteilung per WebSocket
- Frontend mit Zoom/Pan (auch Pinch am Handy) und den Werkzeugen Pinsel, Pipette und Info
- Gilden: Seite, Folgen, Gründen, Beitrittslink, HSV-Farbwahl
- Oberfläche Englisch/Deutsch, Guthaben über den `FakeProvider`

Leaderboard, Fraktionsansicht und OBS-Overlay gehören zu Phase 2 und fehlen noch.

## Lokal starten

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn pixelwar.main:create_app --factory --reload
```

Dann http://localhost:8000 öffnen, einen Namen eingeben, „Add credit“ klicken
(im Dev-Modus wird sofort gutgeschrieben) und malen.

| Seite | Wofür |
|---|---|
| `/` | Leinwand |
| `/guilds` | Gildenliste und Gilde gründen |
| `/c/<slug>` | Gildenseite und Beitrittslink |
| `/admin` | Preisparameter (Benutzer `admin`, Passwort im Dev-Modus `admin`) |

Tests: `pytest`

Die Datenbank von vor dem Umbau (`pixelkrieg.db`) passt nicht mehr zum neuen
Schema und kann gelöscht werden.

## Aufbau

| Datei | Aufgabe |
|---|---|
| `config.toml` | **Alle** Einstellungen: Eventzeiten, Standardpreise, Limits, Palette |
| `pixelwar/pricing.py` | Die Preisformel (einziger Ort, an dem Preise entstehen) und die Laufzeit-Parameter |
| `pixelwar/game.py` | Spielkern: malen, Ansteckung, Abkühlung, Parameter ändern, Guthaben |
| `pixelwar/guilds.py` | Gilden: gründen, beitreten, folgen, Bild |
| `pixelwar/realtime.py` | WebSocket-Verteilung mit 100-ms-Bündelung |
| `pixelwar/main.py` | FastAPI: alle Endpunkte, Hintergrund-Jobs, Admin-Zugang |
| `pixelwar/db.py` | Schema, Verbindung, Transaktionen |
| `pixelwar/payments.py` | `PaymentProvider`-Schnittstelle und `FakeProvider` |
| `pixelwar/ratelimit.py`, `event.py`, `auth.py`, `config.py` | Rate-Limit, Event-Zeitraum, Login-Cookie, Config |
| `static/i18n/en.json`, `de.json` | **Alle** Oberflächentexte |
| `static/common.js` | Sprache, Geldformat, Kopfzeile, Login, Aufladen |
| `static/app.js` | Leinwand |
| `static/guild.js` | Gildenseiten inkl. HSV-Farbregler |

Neue Sprache: `static/i18n/<code>.json` anlegen (gleiche Schlüssel wie `en.json`)
und den Code in `LANGS` in `common.js` eintragen.

## Entscheidungen, die die Spec offen ließ

- **Farbindex 0 ist nicht malbar.** 0 = „leer“. Sonst könnte man ein Pixel „leeren“,
  und der Nächste würde es zum Grundpreis übermalen.
- **Ansteckung trifft alle 8 Nachbarn, auch leere.** Leere Pixel kosten trotzdem
  immer den Grundpreis. Die gesammelte Hitze zählt erst, wenn das Pixel bemalt und
  danach übermalt wird.
- **Abkühlung:** −1 heat pro vollen 30 Minuten ohne Bemalung *dieses* Pixels.
  Ansteckung setzt die Abkühl-Uhr nicht zurück.
- **Die 25-€-Grenze gilt für die Summe aller Aufladungen**, nicht für das aktuelle Guthaben.
- **Preisschutz beim Malen:** Ist der Preis zwischen Anzeige und Klick gestiegen, lehnt
  der Server ab (409), statt still mehr abzubuchen.
- **Beitreten und Folgen gehen auch vor dem Eventstart**, damit Streamer vorher ihre
  Community sammeln können. Gründen kostet Geld und geht deshalb nur während des Events.
- **Gildenbilder** werden serverseitig geprüft, auf 256×256 zugeschnitten und neu als PNG
  gespeichert. Dabei gehen versteckte Metadaten wie GPS-Daten aus Handyfotos verloren.
- **Das Admin-Panel** nutzt ein eigenes Passwort (HTTP Basic Auth), getrennt vom
  Spieler-Login.

## Betrieb

- **Genau ein Prozess** (`uvicorn` ohne `--workers`). Leinwand, Preise, Rate-Limit und
  WebSocket-Clients liegen im Speicher.
- `deploy/Caddyfile` (HTTPS für pixelwar.gg) und `deploy/pixelwar.service` (systemd) liegen bei.
- In Produktion `dev_mode = false` setzen und `PIXELWAR_SECRET` sowie
  `PIXELWAR_ADMIN_PASSWORD` als Umgebungsvariablen vergeben. Ohne beides startet der
  Server nicht.

## Offen vor dem Livegang

- **Echter Login fehlt.** Bisher gibt es nur den Dev-Login (Name eintippen).
  Vorschlag: „Mit Twitch anmelden“.
- **Gildeninhalte moderieren.** Name, Beschreibung und Bild sind Nutzerinhalte
  (siehe SPEC, Abschnitt 6). Die automatische Prüfung kommt in Phase 3.
- **USA als Zielmarkt:** Für unter 13-Jährige gilt dort COPPA. Das sollte rechtlich
  geprüft werden, zusätzlich zur bisherigen Altersabfrage (18 oder Erlaubnis der Eltern).

# Pixelkrieg

Bezahlte Pixel-Leinwand als zeitlich begrenztes Event. Spezifikation: siehe SPEC.

**Stand: Phase 1 (Spielkern) fertig.** Leinwand, Zonenpreise, Abkühlung,
Live-Verteilung per WebSocket, Frontend mit Zoom/Pan (auch Pinch am Handy),
Guthaben über den `FakeProvider`.

## Lokal starten

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn pixelkrieg.main:create_app --factory --reload
```

Dann http://localhost:8000 öffnen, einen Namen eingeben, „Aufladen“ klicken
(im Dev-Modus wird sofort gutgeschrieben) und malen.

**Phase-1-Test „zwei Rechner“:** Mit `--host 0.0.0.0` starten und vom Handy im
selben WLAN `http://<IP-des-Rechners>:8000` öffnen. Beide malen in dieselbe
Zone und schalten „Preiskarte“ ein. Man sieht die Pixel des anderen live, und
der Preis steigt bei jedem Übermalen.

Tests: `pytest`

## Aufbau

| Datei | Aufgabe |
|---|---|
| `config.toml` | **Alle** Einstellungen: Eventzeiten, Preise, Limits, Palette |
| `pixelkrieg/config.py` | Config laden und prüfen |
| `pixelkrieg/db.py` | Schema, Verbindung, Transaktionen |
| `pixelkrieg/game.py` | Spielkern: malen, abkühlen, Guthaben gutschreiben |
| `pixelkrieg/pricing.py` | Die Preisformel (einziger Ort, an dem Preise entstehen) |
| `pixelkrieg/realtime.py` | WebSocket-Verteilung mit 100-ms-Bündelung |
| `pixelkrieg/payments.py` | `PaymentProvider`-Schnittstelle + `FakeProvider` |
| `pixelkrieg/ratelimit.py` | 5 Pixel/Sekunde pro Account |
| `pixelkrieg/event.py` | Event-Zeitraum und Nachtpause |
| `pixelkrieg/auth.py` | Signiertes Login-Cookie |
| `pixelkrieg/main.py` | FastAPI: Endpunkte, Hintergrund-Jobs |
| `static/` | Frontend (Vanilla JS) |

Die wichtigsten Designentscheidungen stehen als Kommentare direkt im Code,
vor allem am Anfang von `game.py`, `realtime.py` und `static/app.js`.

## Entscheidungen, die die Spec offen ließ

- **Farbindex 0 ist nicht malbar.** 0 = „leer“. Dürfte man 0 malen, könnte man
  ein Pixel „leeren“, und der Nächste würde es ohne Heat-Anstieg übermalen.
  Malbar sind die Indizes 1–31, darunter auch Weiß.
- **Die Obergrenze von 25 € gilt für die Summe aller Aufladungen**, nicht für das
  aktuelle Guthaben. Sonst könnte man 25 € ausgeben, erneut 25 € aufladen und so
  weiter. Das widerspricht dem Jugendschutz-Zweck.
- **Preisschutz beim Malen:** Das Frontend schickt den angezeigten Preis mit
  (`max_price_cents`). Ist die Zone inzwischen teurer, lehnt der Server mit 409
  ab, statt still mehr abzubuchen.
- **Gleiche Farbe auf eigenes Pixel** wird abgelehnt (Doppelklick-Schutz).
- **Nachtpause** sperrt Malen *und* Aufladen.
- Die Spalte `zones.frozen` ist schon angelegt (für Phase 3), damit keine
  Migration nötig wird.

## Betrieb

- **Genau ein Prozess** (`uvicorn` ohne `--workers`). Leinwand, Rate-Limit und
  WebSocket-Clients liegen im Speicher. Mehrere Prozesse würden sich
  widersprechen. Ob ein Prozess für 2.000 Nutzer reicht, klärt der Lasttest in
  Phase 5.
- `deploy/Caddyfile` (HTTPS) und `deploy/pixelkrieg.service` (systemd) liegen bei.
- In Produktion `dev_mode = false` setzen und `PIXELKRIEG_SECRET` als
  Umgebungsvariable vergeben. Ohne Secret startet der Server nicht.

## Offen vor dem Livegang

- **Echter Login fehlt.** Die Spec sagt nicht, wie sich Nutzer anmelden. Phase 1
  hat nur einen Dev-Login (Name eintippen). Naheliegend wäre „Mit Twitch
  anmelden“, weil das Publikum ohnehin dort ist.
- Phase 2: Clans und Leaderboard. Phase 3: Moderation. Phase 4: Stripe/PayPal.
  Phase 5: Härtung und Lasttest.

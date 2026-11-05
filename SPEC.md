# SPEC — Pixel War

Domain: **pixelwar.gg**

Eine bezahlte Pixel-Leinwand als zeitlich begrenztes Event. Nutzer kaufen Guthaben und malen
einzelne Pixel. Je umkämpfter ein Pixel, desto teurer wird es. Gilden (meist
Streamer-Communities) kämpfen um Territorium, ein Live-Leaderboard zeigt den Stand.

Zielgruppe ist international, vor allem die USA. Die Oberfläche ist standardmäßig englisch,
abgerechnet wird in Euro.

**Wichtig für die Umsetzungsreihenfolge:** Die echte Zahlungsanbindung kommt ZULETZT. Die
Gewerbeanmeldung und die PayPal-/Stripe-Verifizierung laufen parallel und dauern Wochen.
Bis dahin wird mit einem Fake-Zahlungsanbieter entwickelt (siehe Phase 4).

---

## 1. Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn
- **Echtzeit:** WebSockets (FastAPI eingebaut)
- **Datenbank:** SQLite mit WAL-Modus (reicht für Version 1; später auf Postgres migrierbar —
  deshalb SQL sauber halten, keine SQLite-Spezialitäten)
- **Canvas im Speicher:** NumPy-Array `uint8[400][400]`, Index in eine Farbpalette.
  Die Datenbank ist die Wahrheit, das Array der schnelle Zugriff. Beim Start aus DB laden.
- **Frontend:** Vanilla JavaScript, HTML5 `<canvas>`, kein Framework.
  Muss auf dem Handy funktionieren (Pinch-Zoom, Pan).
- **Deployment:** Ein Linux-Server, Caddy als Reverse-Proxy mit automatischem HTTPS.

Der Entwickler kennt Python-Grundlagen, aber kein FastAPI und kein async. Code bitte
kommentieren und erklären, warum etwas so gebaut ist — nicht nur was.

---

## 2. Kernmechanik

### Leinwand
- 400 × 400 Pixel = 160.000 Pixel
- Feste Farbpalette mit 32 Farben (Index 0 = leer/weiß)
- Jeder Spieler darf jede Farbe malen (freie Farbwahl, NICHT an die Gilde gebunden)

### Preis pro Pixel
Jedes Pixel hat einen eigenen Wert `heat` (Fließkommazahl, Start 0).

```
preis_in_cent = round(grundpreis * (faktor ** heat))      # Standard: round(2 * 1.15 ** heat)
```

Regeln:
- **Leeres Pixel bemalen:** kostet immer den Grundpreis (2 ct), `heat` bleibt unverändert.
  Weiße Flächen sollen billig bleiben, auch wenn daneben gekämpft wird.
- **Bemaltes Pixel übermalen:** kostet den aktuellen Preis, danach `heat += 1`.
- **Nachbar-Ansteckung:** Beim Übermalen bekommen die 8 direkten Nachbarn jeweils
  `heat += ansteckung` (Standard 0,02 = 2 % der Erhitzung).
- **Abkühlung:** Für jedes Pixel gilt: pro 30 Minuten ohne Bemalung `heat = max(0, heat - 1)`.

Alle Preiswerte sind **Ganzzahlen in Cent**. `heat` ist eine Kommazahl, der daraus berechnete
Preis wird sofort zu ganzen Cent gerundet. Niemals Fließkommazahlen für Geld.

### Zur Laufzeit einstellbare Parameter
Diese Werte werden während des Events nachjustiert und müssen im Admin-Panel **ohne Neustart**
änderbar sein:

| Parameter | Standard |
|---|---|
| Grundpreis | 2 ct |
| Eskalationsfaktor | 1,15 |
| Nachbar-Ansteckung | 0,02 |
| Abkühlzeit | 30 min |

Die Standardwerte stehen in der Konfigurationsdatei. Im Admin-Panel geänderte Werte werden in
der Datenbank gespeichert und überleben einen Neustart. Eine Änderung wirkt sofort auf alle
Pixel, und alle Clients bekommen die neuen Preise.

### Limits
- Max. **5 Pixel pro Sekunde pro Account** (serverseitig erzwingen, nicht im Frontend)
- Guthaben kann nicht negativ werden — Prüfung und Abbuchung in **einer** Transaktion,
  sonst kann man sich per Doppelklick ins Minus malen
- Summe aller Aufladungen pro Account begrenzt (konfigurierbar, Standard 25 €).
  Grund: Das Publikum sind Twitch-Chats mit vielen Minderjährigen.

### Event-Zeitraum
- Feste Start- und Endzeit in der Konfiguration
- Vor dem Start: Countdown-Seite, keine Käufe möglich
- Nach dem Ende: Leinwand eingefroren, nur noch anschaubar, PNG-Export in voller Auflösung
- Optional konfigurierbar: Nachtpause (z. B. 00:00–08:00 keine Käufe)

---

## 3. Bedienung

### Zoom
- Minimaler Zoom: Die gesamte Leinwand füllt das Fenster. Weiter rauszoomen ist nicht möglich.
- Reinzoomen bleibt frei.

### Werkzeuge
Werkzeugleiste mit drei Modi:
- **Pinsel:** malen
- **Pipette:** Farbe vom Canvas aufnehmen
- **Info:** Ein Pixel antippen öffnet ein Fenster mit aktuellem Preis, Anzahl der Übermalungen,
  Zeitpunkt der letzten Bemalung, Nutzer und Gilde

Zusätzlich steht beim Überfahren mit der Maus unten links immer die Koordinate und der
aktuelle Preis des Pixels darunter.

### Sprache und Währung
- Oberfläche standardmäßig auf **Englisch**, per Schalter auf Deutsch umstellbar
- Alle Texte kommen aus einer Sprachdatei pro Sprache, nicht fest im Code
- Abgerechnet und angezeigt wird in **Euro**

---

## 4. Gilden

Gilden funktionieren wie Kanäle, nicht wie stumme Teams.

- **Gildenseite** mit Name, Beschreibung, Bild, Mitgliederzahl, Follower-Zahl und
  gehaltenen Pixeln
- Man kann einer Gilde **folgen**, ohne Mitglied zu sein
- Jeder Nutzer kann eine Gilde gründen. Das kostet **5 €**, die vom Guthaben abgezogen werden.
- Die **Gildenfarbe** wird über einen HSV-Regler (Farbton / Sättigung / Helligkeit) frei
  gewählt, nicht aus einer festen Liste. Sie gilt NUR fürs Leaderboard und die
  Fraktionsansicht, nicht fürs Malen.
- **Beitritt per Link:** `/c/<slug>` — ein Klick, kein Code, keine Auswahlliste.
  Das ist der wichtigste Punkt: Der Streamer pinnt den Link in den Chat und muss nichts erklären.
- **Kein Gildenwechsel.** Einmal beigetreten, dauerhaft. Sonst laufen kurz vor Schluss alle
  zum Gewinner über.
- Ein Pixel gehört der Gilde desjenigen, der es zuletzt gemalt hat

### Leaderboard
Zwei Wertungen nebeneinander:
1. **Gehaltene Pixel** (absolut und in Prozent) — kann fallen, das erzeugt die Dramatik
2. **Größtes zusammenhängendes Gebiet** — damit auch kleine, disziplinierte Communities
   gewinnen können und nicht dauerhaft bei 0,4 % feststecken

Dazu innerhalb jeder Gilde eine **Mitgliederliste** nach gehaltenen Pixeln sortiert.
Die interne Konkurrenz ist erfahrungsgemäß der stärkere Antrieb.

Das Leaderboard wird alle 5 Sekunden neu berechnet und über WebSocket verteilt (nicht bei
jedem Pixel, das hält der Server sonst nicht aus).

---

## 5. Fraktionsansicht

Ein Umschalter im Frontend, zwei Darstellungen derselben Daten:
- **Kunstansicht:** die echten gemalten Farben
- **Fraktionsansicht:** jedes Pixel in der Gildenfarbe seines aktuellen Besitzers

Die Fraktionsansicht ist für den Twitch-Stream und das OBS-Overlay gedacht — man sieht
Fronten wandern wie auf einer Risiko-Karte.

---

## 6. Moderation (sicherheitskritisch, nicht optional)

Betreiber haftet für rechtswidrige Inhalte (§ 86a StGB). Das System muss **ohne menschliches
Eingreifen handeln können**, auch nachts.

### Schicht 1 — Mustervergleich (bei jedem gesetzten Pixel)
Deterministischer Abgleich gegen bekannte verbotene Symbole als Pixelmuster, in allen
4 Rotationen und gespiegelt. Läuft in Millisekunden, kostenlos, keine KI.
Prüft nur das lokale Umfeld des neu gesetzten Pixels.

### Schicht 2 — Bildprüfung (jede Minute)
Die Leinwand in **8 Kacheln zerlegen**, jede hochskaliert an ein Vision-Modell schicken.
Das Gesamtbild als Ganzes zu schicken funktioniert nicht — kleine Symbole gehen darin unter.
Erkennt, was Muster nicht erwischen: Schriftzüge, Andeutungen, obszöne Darstellungen.

### Gildeninhalte
Gildenname, Beschreibung und Gildenbild sind ebenfalls nutzererzeugte Inhalte und brauchen
dieselbe Absicherung (Bildprüfung beim Hochladen, Admin kann Bild und Text entfernen).

### Reaktion bei Verdacht (automatisch, sofort)
1. Betroffener Bereich wird **eingefroren und übermalt**
2. Alarm per ntfy.sh oder Pushover mit höchster Priorität (durchbricht Stummschaltung)
3. Eintrag im Admin-Log mit Vorher-Bild, Zeitstempel, Account, IP

Ein Admin gibt Fehlalarme hinterher wieder frei. Lieber zehn Fehlalarme mit kurz grauem
Bereich als ein Treffer, der 40 Minuten live im Stream steht.

### Admin-Panel
- Preisparameter zur Laufzeit ändern (siehe 2.)
- Live-Log aller Auslöser
- Bereich einfrieren / freigeben
- Rechteckigen Bereich übermalen
- Account sperren (sperrt sofort, Restguthaben bleibt erhalten und wird auf Anfrage
  ausgezahlt — eine Verfallsklausel wäre nach § 307 BGB angreifbar)
- Empfindlichkeit der Prüfung einstellbar

---

## 7. Datenmodell

```sql
users            (id, handle, email, guild_id, balance_cents, is_banned, created_at)
guilds           (id, slug, name, description, color_hex, image_version, owner_user_id, created_at)
guild_followers  (guild_id, user_id, created_at)                          -- PK (guild_id, user_id)
pixels           (x, y, color_index, owner_user_id, guild_id, heat, overpaint_count,
                  updated_at, cooled_at)                                   -- PK (x, y)
game_params      (key, value)                                              -- zur Laufzeit geänderte Preisparameter
paint_log        (id, x, y, color_index, user_id, guild_id, price_cents, created_at)
topups           (id, user_id, amount_cents, provider, provider_ref, status, created_at)
mod_events       (id, kind, x, y, detail, resolved, created_at)
```

Die frühere Tabelle `zones` entfällt, `heat` liegt jetzt pro Pixel in `pixels`.

`paint_log` ist vollständig und wird nie gelöscht — daraus lässt sich das Event später als
Zeitraffer-Video rendern. Das ist wertvolles Material für den Rückblick.

---

## 8. API

```
GET  /api/canvas              -> komprimierter Schnappschuss der Farben (Binärformat, nicht JSON)
GET  /api/prices              -> komprimierter Schnappschuss aller Pixelpreise (Binärformat)
GET  /api/pixel?x=&y=         -> Details zu einem Pixel (Info-Werkzeug)
POST /api/paint               -> {x, y, color_index}
GET  /api/leaderboard
GET  /api/me
POST /api/topup               -> startet Zahlung
GET  /api/guilds              -> Liste
POST /api/guilds              -> Gilde gründen (kostet 5 €)
GET  /api/guilds/<slug>       -> Gildenseite
POST /api/guilds/<slug>/join
POST /api/guilds/<slug>/follow   /   DELETE /api/guilds/<slug>/follow
GET  /c/<slug>                -> Beitrittsseite
GET  /admin                   -> Admin-Panel
GET  /overlay                 -> OBS-taugliche Seite, transparenter Hintergrund
WS   /ws                      -> Live-Updates
```

WebSocket-Nachrichten an alle:
- `pixel` — Pixel geändert (gebündelt alle 100 ms senden, nicht einzeln)
- `price` — Pixelpreise geändert (gebündelt, nur Pixel, deren Preis sich wirklich ändert)
- `prices_reset` — Preisparameter geändert, alle Preise neu laden
- `leaderboard` — alle 5 Sekunden
- `freeze` — Bereich gesperrt

---

## 9. Zahlungen

Hinter einer **Schnittstelle** bauen, nicht direkt gegen einen Anbieter:

```python
class PaymentProvider(Protocol):
    def create_checkout(self, user_id: int, amount_cents: int) -> str: ...
    def verify_webhook(self, body: bytes, headers: dict) -> TopupResult: ...
```

Drei Implementierungen:
1. `FakeProvider` — schreibt Guthaben sofort gut, **nur im Entwicklungsmodus**.
   Damit wird das komplette Spiel gebaut und getestet, bevor irgendein Konto existiert.
2. `StripeProvider`
3. `PayPalProvider`

Guthaben-Pakete: 3 €, 10 €, 25 €. Abgerechnet wird in Euro. Niemals pro Pixel abbuchen —
die Gebühren (ca. 0,35 € + 2,49 % pro Zahlung) würden Kleinbeträge auffressen.

Webhooks müssen **idempotent** sein: dieselbe Zahlung darf bei doppelter Zustellung nicht
zweimal gutgeschrieben werden (`provider_ref` mit Unique-Index).

Beim Aufladen: Altersabfrage und deutlicher Hinweis, dass unter 18 die Erlaubnis der Eltern
nötig ist.

---

## 10. Umsetzungsreihenfolge

**Phase 1 — Spielkern** ✅
Canvas im Speicher, Pixel setzen, Pixelpreise mit Ansteckung, Abkühlung,
WebSocket-Verteilung, Frontend mit Zoom und Pan, Werkzeuge (Pinsel, Pipette, Info),
Sprachumschaltung, zur Laufzeit einstellbare Preisparameter.
Guthaben als einfache Zahl, `FakeProvider`.
Ziel: Auf zwei Rechnern gleichzeitig malen und die Preise steigen sehen.

**Phase 2 — Gilden & Leaderboard**
Gildenseiten, Folgen, Gründen, Beitrittslinks (vorgezogen, siehe unten), beide Wertungen,
Fraktionsansicht, OBS-Overlay.

**Phase 3 — Moderation**
Mustervergleich, Kachelprüfung, Prüfung von Gildeninhalten, Auto-Freeze, Alarme,
Admin-Panel (Rest).

**Phase 4 — Zahlungen**
Stripe und PayPal. Erst wenn die Konten verifiziert sind.

**Phase 5 — Härtung**
Lasttest mit simulierten Nutzern (Ziel: 2.000 gleichzeitig), Rate-Limits prüfen,
Countdown- und Endseite, PNG-Export, Zeitraffer aus `paint_log`.

Nach jeder Phase soll das Ding lauffähig sein. Keine halbfertigen Zwischenstände.

---

## 11. Nicht vergessen

- Alle Beträge als Ganzzahlen in Cent
- Preis- und Guthabenprüfung serverseitig, das Frontend darf nie Preise berechnen
- Zeitstempel in UTC speichern
- Konfiguration (Eventzeiten, Standard-Preise, Limits) in einer Datei, nicht im Code verstreut
- Alle Oberflächentexte in Sprachdateien
- Der Lasttest ist kein Extra. Wenn der Server beim Ansturm umfällt, ist das Event vorbei.

"""Live-Updates per WebSocket.

Zwei Ideen machen das bei vielen Zuschauern bezahlbar:

1. **Bündeln.** Pixeländerungen werden nicht einzeln verschickt, sondern
   gesammelt und alle 100 ms als ein Paket. Malt jemand zweimal auf dasselbe
   Pixel, geht nur der letzte Stand raus. Jedes Paket wird EINMAL zu JSON
   gewandelt und dann an alle verschickt.

2. **Eine Warteschlange pro Client.** Ein Zuschauer mit schlechtem Handynetz
   darf nicht alle anderen ausbremsen. Jeder Client hat eine eigene, begrenzte
   Warteschlange, die ein eigener kleiner Task abarbeitet. Läuft sie über, wird
   der Client getrennt – sein Browser verbindet sich neu und lädt den aktuellen
   Stand komplett. Das ist billiger, als für ihn endlos Nachrichten zu puffern.

Versionsnummern: Jedes Paket hat eine fortlaufende Nummer `v`. Der Schnappschuss
unter /api/canvas nennt die Nummer, auf deren Stand er mindestens ist. Der Browser
verwirft Pakete, die älter sind – so passen Schnappschuss und Live-Strom lückenlos
zusammen (Details in static/app.js, Funktion `resync`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


class Client:
    def __init__(self, ws: WebSocket, queue_size: int):
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.overflowed = False

    def offer(self, message: str) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.overflowed = True


class Hub:
    def __init__(self, flush_interval_s: float = 0.1, client_queue_size: int = 100):
        self.flush_interval_s = flush_interval_s
        self.client_queue_size = client_queue_size
        self.clients: set[Client] = set()
        self.version = 0
        # Dicts statt Listen: Mehrere Änderungen am selben Pixel im selben
        # 100-ms-Fenster überschreiben sich, nur der letzte Stand zählt.
        self._pixels: dict[tuple[int, int], int] = {}
        self._prices: dict[tuple[int, int], int] = {}
        self._prices_reset = False

    # -- Aufrufe aus dem Spielkern ------------------------------------------------

    def queue_pixel(self, x: int, y: int, color_index: int) -> None:
        self._pixels[(x, y)] = color_index

    def queue_price(self, x: int, y: int, price_cents: int) -> None:
        self._prices[(x, y)] = price_cents

    def queue_prices_reset(self) -> None:
        """Alle Preise haben sich geändert (neue Parameter im Admin-Panel).

        Statt 160.000 Einzelpreise zu schicken, sagen wir den Browsern nur
        "bitte /api/prices neu laden".
        """
        self._prices_reset = True

    # -- Verteilen ----------------------------------------------------------------

    def flush(self) -> None:
        if not self._pixels and not self._prices and not self._prices_reset:
            return
        self.version += 1
        messages = []
        if self._pixels:
            # Flaches Array [x, y, farbe, x, y, farbe, ...] – deutlich kürzer als
            # eine Liste von Objekten mit Feldnamen.
            data = [v for (x, y), c in self._pixels.items() for v in (x, y, c)]
            messages.append({"type": "pixel", "v": self.version, "data": data})
        if self._prices_reset:
            # Einzelpreise sind durch den Neuladen-Befehl überholt.
            messages.append({"type": "prices_reset", "v": self.version})
        elif self._prices:
            data = [v for (x, y), p in self._prices.items() for v in (x, y, p)]
            messages.append({"type": "price", "v": self.version, "data": data})
        self._pixels = {}
        self._prices = {}
        self._prices_reset = False
        for msg in messages:
            self.broadcast(msg)

    def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg, separators=(",", ":"))
        for client in list(self.clients):
            client.offer(text)

    async def run(self) -> None:
        """Hintergrund-Task: alle `flush_interval_s` Sekunden ein Paket verschicken."""
        while True:
            await asyncio.sleep(self.flush_interval_s)
            try:
                self.flush()
            except Exception:  # pragma: no cover – darf die Schleife nie beenden
                log.exception("flush fehlgeschlagen")

    # -- Eine einzelne WebSocket-Verbindung --------------------------------------

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        client = Client(ws, self.client_queue_size)
        self.clients.add(client)
        sender = asyncio.create_task(self._send_loop(client))
        try:
            # Wir erwarten vom Browser keine Nachrichten (gemalt wird per HTTP),
            # lesen aber trotzdem, um mitzubekommen, wann er die Verbindung schließt.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def _send_loop(self, client: Client) -> None:
        try:
            while True:
                if client.overflowed:
                    await client.ws.close(code=4000, reason="zu langsam, bitte neu laden")
                    return
                msg = await client.queue.get()
                await client.ws.send_text(msg)
        except Exception:
            # Verbindung ist weg; serve() räumt auf.
            with contextlib.suppress(Exception):
                await client.ws.close()

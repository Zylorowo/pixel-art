"""Rate-Limit: höchstens N Pixel pro Sekunde pro Account.

Wird serverseitig erzwungen – eine Grenze im Frontend umgeht jeder mit einem
kleinen Skript.

Der Zustand liegt im Speicher. Das funktioniert, weil der Server als EIN Prozess
läuft (siehe README: uvicorn ohne --workers). Mit mehreren Prozessen hätte jeder
seinen eigenen Zähler und das Limit wäre wirkungslos.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Hashable


class SlidingWindowLimiter:
    """Erlaubt höchstens `limit` Aufrufe in jedem beliebigen Zeitfenster von `window` Sekunden.

    Ein "gleitendes Fenster" statt "pro Kalendersekunde": Sonst könnte man bei
    x.999 fünf und bei x+1.001 nochmal fünf Pixel setzen – zehn in 2 ms.
    """

    def __init__(self, limit: int, window: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[Hashable, deque[float]] = {}

    def allow(self, key: Hashable) -> bool:
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        # Alte Zeitstempel, die aus dem Fenster gefallen sind, wegwerfen.
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

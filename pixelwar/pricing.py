"""Preisformel und Preisparameter. Der einzige Ort, an dem Preise berechnet werden.

Das Frontend berechnet NIE selbst Preise – es zeigt nur an, was der Server sagt.

Es gibt genau EINE Implementierung der Formel (`price_array`), die mit NumPy
auf beliebig vielen Pixeln gleichzeitig rechnet. Auch der Preis eines einzelnen
Pixels wird darüber berechnet. Hätten wir eine zweite Variante mit Pythons
`round()`, könnten beide in seltenen Grenzfällen um einen Cent abweichen – und
dann würde die angezeigte Preiskarte nicht zum abgebuchten Preis passen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

# Obergrenze, damit extreme Parameter (Faktor 3, heat 500) keine unendlich
# großen Zahlen erzeugen. Weit jenseits dessen, was jemand bezahlen kann.
MAX_PRICE_CENTS = 1_000_000_000


@dataclass(frozen=True)
class PriceParams:
    """Die vier Stellschrauben, die während des Events im Admin-Panel geändert werden."""

    base_cents: int = 2  # Grundpreis in Cent
    factor: float = 1.15  # Eskalationsfaktor pro heat-Stufe
    contagion: float = 0.02  # heat, den jeder der 8 Nachbarn beim Übermalen bekommt
    cooldown_minutes: int = 30  # pro so vielen Minuten ohne Bemalung: heat - 1

    def validate(self) -> None:
        # Grenzen, die Tippfehler im Admin-Panel abfangen ("115" statt "1.15").
        if not (isinstance(self.base_cents, int) and 1 <= self.base_cents <= 10_000):
            raise ValueError("base_cents: ganze Zahl zwischen 1 und 10000")
        if not 1.0 <= self.factor <= 3.0:
            raise ValueError("factor: zwischen 1.0 und 3.0")
        if not 0.0 <= self.contagion <= 1.0:
            raise ValueError("contagion: zwischen 0 und 1")
        if not (isinstance(self.cooldown_minutes, int) and 1 <= self.cooldown_minutes <= 24 * 60):
            raise ValueError("cooldown_minutes: ganze Zahl zwischen 1 und 1440")

    def as_dict(self) -> dict:
        return asdict(self)


def price_array(heat: np.ndarray, empty: np.ndarray, params: PriceParams) -> np.ndarray:
    """Preise in Cent (int64) für viele Pixel auf einmal.

    preis = round(grundpreis * faktor ** heat), leere Pixel immer zum Grundpreis.
    `faktor ** heat` ist kurz eine Kommazahl, wird aber sofort zu ganzen Cent
    gerundet. Gespeichert, verglichen und abgebucht werden nur Ganzzahlen.
    """
    with np.errstate(over="ignore"):
        raw = params.base_cents * np.power(params.factor, heat, dtype=np.float64)
    prices = np.minimum(np.rint(raw), MAX_PRICE_CENTS).astype(np.int64)
    return np.where(empty, params.base_cents, prices)


def price_cents(heat: float, empty: bool, params: PriceParams) -> int:
    """Preis eines einzelnen Pixels – über dieselbe Formel wie `price_array`."""
    return int(price_array(np.array([heat]), np.array([empty]), params)[0])

"""Preisformel. Der einzige Ort, an dem Preise berechnet werden.

Das Frontend berechnet NIE selbst Preise – es zeigt nur an, was der Server sagt.
"""


def price_cents(heat: int, base_cents: int = 2, factor: float = 1.15) -> int:
    """preis_in_cent = round(base * factor ** heat), als Ganzzahl.

    `factor ** heat` ist kurz eine Kommazahl, wird aber sofort zu ganzen Cent
    gerundet. Gespeichert, verglichen und abgebucht werden nur Ganzzahlen.
    """
    return int(round(base_cents * factor**heat))

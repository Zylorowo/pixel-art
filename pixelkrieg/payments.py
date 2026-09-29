"""Zahlungsanbieter hinter einer gemeinsamen Schnittstelle.

Das Spiel kennt nur `PaymentProvider`. Ob dahinter Stripe, PayPal oder der
FakeProvider steckt, ist ihm egal. So kann das komplette Spiel gebaut und
getestet werden, bevor ein echtes Händlerkonto existiert (Phase 4).

Ablauf einer Aufladung (bei allen Anbietern gleich):
1. POST /api/topup → `create_checkout` liefert eine URL zur Bezahlseite.
2. Der Nutzer bezahlt beim Anbieter.
3. Der Anbieter ruft unseren Webhook auf → `verify_webhook` prüft die Signatur
   und liefert ein `TopupResult`.
4. `Game.credit_topup` schreibt idempotent gut.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TopupResult:
    provider_ref: str  # Eindeutige Zahlungs-ID beim Anbieter
    user_id: int
    amount_cents: int
    paid: bool


class InvalidWebhook(Exception):
    pass


class PaymentProvider(Protocol):
    name: str

    def create_checkout(self, user_id: int, amount_cents: int) -> str: ...

    def verify_webhook(self, body: bytes, headers: dict) -> TopupResult: ...


class FakeProvider:
    """Nur für die Entwicklung: "bezahlt" sofort, ohne echtes Geld.

    Damit trotzdem der echte Weg getestet wird, erzeugt er einen signierten
    Webhook, genau wie ein echter Anbieter ihn schicken würde. Der Server
    verarbeitet ihn über denselben Code wie später bei Stripe/PayPal.
    """

    name = "fake"
    SIGNATURE_HEADER = "x-fake-signature"

    def __init__(self, secret: str):
        self._secret = secret.encode()
        self.outbox: list[tuple[bytes, dict]] = []

    def create_checkout(self, user_id: int, amount_cents: int) -> str:
        body = json.dumps(
            {"ref": "fake_" + secrets.token_hex(8), "user_id": user_id, "amount_cents": amount_cents}
        ).encode()
        self.outbox.append((body, {self.SIGNATURE_HEADER: self._sign(body)}))
        # Eine echte Bezahlseite gibt es nicht – zurück zur Startseite.
        return "/?topup=done"

    def pop_webhooks(self) -> list[tuple[bytes, dict]]:
        out, self.outbox = self.outbox, []
        return out

    def verify_webhook(self, body: bytes, headers: dict) -> TopupResult:
        sig = {k.lower(): v for k, v in headers.items()}.get(self.SIGNATURE_HEADER, "")
        # compare_digest statt ==: vergleicht in konstanter Zeit, damit man die
        # Signatur nicht Zeichen für Zeichen über Antwortzeiten erraten kann.
        if not hmac.compare_digest(sig, self._sign(body)):
            raise InvalidWebhook("Signatur ungültig")
        data = json.loads(body)
        return TopupResult(
            provider_ref=data["ref"],
            user_id=int(data["user_id"]),
            amount_cents=int(data["amount_cents"]),
            paid=True,
        )

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()


# StripeProvider und PayPalProvider folgen in Phase 4, sobald die Konten
# verifiziert sind. Sie implementieren dieselben zwei Methoden.

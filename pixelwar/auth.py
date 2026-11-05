"""Login-Cookie: signierte User-ID.

Das Cookie enthält `<user_id>.<signatur>`. Die Signatur ist ein HMAC mit dem
geheimen Schlüssel aus der Config. Ohne den Schlüssel kann niemand ein gültiges
Cookie für eine fremde ID bauen – deshalb braucht es dafür keine eigene Tabelle.

Phase 1 hat nur einen Dev-Login (Name eingeben, fertig). Ein echter Login
(z. B. "Mit Twitch anmelden") muss vor dem Livegang noch dazu.
"""

from __future__ import annotations

import hashlib
import hmac

COOKIE_NAME = "pw_session"


def make_token(secret: str, user_id: int) -> str:
    return f"{user_id}.{_sign(secret, str(user_id))}"


def read_token(secret: str, token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    uid, sig = token.split(".", 1)
    if not uid.isdigit() or not hmac.compare_digest(sig, _sign(secret, uid)):
        return None
    return int(uid)


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

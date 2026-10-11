"""Identifiers, record integrity, sealed resumption tickets, log redaction.

No custom cryptography: HMAC-SHA256 from the standard library and AES-GCM
from the `cryptography` package.

Three properties are deliberately kept apart:
  * integrity   - a record/ticket was produced by the key holder and not
                  modified (HMAC tag, AEAD tag);
  * freshness   - it is the *latest* version (epoch/version checks, done
                  elsewhere; a validly tagged old record is still stale);
  * authorization - the principal may act now (authorization.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ID_BYTES = 16  # 128 bits from the OS CSPRNG


def new_identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(ID_BYTES)}"


def new_key() -> bytes:
    return secrets.token_bytes(32)


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class RecordSigner:
    """HMAC-SHA256 over a canonical JSON encoding of a record."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def tag(self, fields: dict[str, Any]) -> str:
        return hmac.new(self._key, _canonical(fields), hashlib.sha256).hexdigest()

    def verify(self, fields: dict[str, Any], tag: str) -> bool:
        return hmac.compare_digest(self.tag(fields), tag or "")


class TicketInvalid(Exception):
    pass


class TicketSealer:
    """AES-256-GCM sealed tickets, readable only by the issuing endpoint.

    A fresh 96-bit random nonce per seal means two tickets for the same
    dialog are different byte strings (no equality linkage on the ticket
    value itself). This is the same general idea as TLS session tickets
    (RFC 5077 / RFC 8446 s4.6.1); it is not a new mechanism.
    """

    def __init__(self, key: bytes, aad_context: str) -> None:
        self._aead = AESGCM(key)
        self._aad = f"agentproto-recovery-ticket-v1|{aad_context}".encode()

    def seal(self, payload: dict[str, Any]) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, _canonical(payload), self._aad)
        return "tkt_" + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")

    def open(self, token: str) -> dict[str, Any]:
        try:
            if not token.startswith("tkt_"):
                raise TicketInvalid("prefix")
            raw = token[4:]
            raw += "=" * (-len(raw) % 4)
            blob = base64.urlsafe_b64decode(raw)
            pt = self._aead.decrypt(blob[:12], blob[12:], self._aad)
            return json.loads(pt)
        except (InvalidTag, ValueError, json.JSONDecodeError) as e:
            raise TicketInvalid(str(e) or type(e).__name__) from e


class Redactor:
    """Keyed, per-domain pseudonyms for log output.

    Keyed per domain on purpose: an unkeyed hash (or a key shared across
    domains) would make the redacted logs of two domains joinable on the
    hash value, which reintroduces the linkage the scoped IDs try to avoid.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def __call__(self, value: str | None) -> str | None:
        if value is None:
            return None
        return "h:" + hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:12]

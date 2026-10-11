"""Mock authorization service.

MOCK TRUST ASSUMPTIONS (documented in docs/threat_model.md):
  * The service is reachable by both the gateway operator and Domain B and
    both trust its answers. In a real deployment each domain would more
    likely verify a delegation token (e.g. OAuth token exchange) locally.
  * Callers pass an already-authenticated principal; this service does not
    authenticate anyone.

Correlation and authorization are independent: holding a valid dialog
mapping or ticket never implies a grant.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

from .errors import AuthzUnavailable
from .failure_injection import FailureInjector, ManualClock

UNAVAILABLE_POINT = "authz.unavailable"


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass
class Grant:
    principal: str
    tenant: str
    actions: frozenset[str]
    expires_at: float
    revoked: bool = False


class AuthorizationService:
    def __init__(self, clock: ManualClock, injector: FailureInjector | None = None) -> None:
        self.clock = clock
        self.injector = injector
        self._grants: dict[tuple[str, str], Grant] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def grant(self, principal: str, tenant: str, actions: set[str] | frozenset[str],
              ttl: float = 3600.0) -> Grant:
        g = Grant(principal, tenant, frozenset(actions), self.clock.now() + ttl)
        with self._lock:
            self._grants[(principal, tenant)] = g
        return g

    def revoke(self, principal: str, tenant: str) -> None:
        with self._lock:
            g = self._grants.get((principal, tenant))
            if g:
                g.revoked = True

    def check(self, principal: str, tenant: str, action: str) -> Decision:
        if self.injector is not None and self.injector.active(UNAVAILABLE_POINT):
            raise AuthzUnavailable()
        with self._lock:
            self.calls += 1
            g = self._grants.get((principal, tenant))
        if g is None or action not in g.actions:
            return Decision.DENIED
        if g.revoked:
            return Decision.REVOKED
        if self.clock.now() >= g.expires_at:
            return Decision.EXPIRED
        return Decision.ALLOW

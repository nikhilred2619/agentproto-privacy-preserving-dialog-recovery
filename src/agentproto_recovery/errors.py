"""Outcome codes and exceptions.

Every recovery attempt ends in exactly one Code. Anything other than
Code.OK is an explicit failure: the system never reports success when it
could not establish the correct dialog association (fail closed).
"""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    OK = "OK"
    # correlation / mapping
    MAPPING_NOT_FOUND = "MAPPING_NOT_FOUND"
    MAPPING_INTEGRITY_FAILURE = "MAPPING_INTEGRITY_FAILURE"
    TICKET_INVALID = "TICKET_INVALID"
    DIALOG_NOT_FOUND = "DIALOG_NOT_FOUND"
    # binding
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PRINCIPAL_MISMATCH = "PRINCIPAL_MISMATCH"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    # lifecycle / freshness
    DIALOG_TERMINATED = "DIALOG_TERMINATED"
    STALE_REFERENCE = "STALE_REFERENCE"
    CONCURRENT_RECOVERY = "CONCURRENT_RECOVERY"
    INVALID_STATE = "INVALID_STATE"
    # authorization (separate from correlation)
    AUTHZ_DENIED = "AUTHZ_DENIED"
    AUTHZ_EXPIRED = "AUTHZ_EXPIRED"
    AUTHZ_REVOKED = "AUTHZ_REVOKED"
    AUTHZ_UNAVAILABLE = "AUTHZ_UNAVAILABLE"
    # infrastructure
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    DOWNSTREAM_UNAVAILABLE = "DOWNSTREAM_UNAVAILABLE"
    GATEWAY_DOWN = "GATEWAY_DOWN"
    # side effects
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class RecoveryError(Exception):
    """Raised inside components; converted to a Code at the boundary."""

    def __init__(self, code: Code, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail


class StoreUnavailable(Exception):
    pass


class AuthzUnavailable(Exception):
    pass


class VersionConflict(Exception):
    pass


class InjectedCrash(Exception):
    """Simulated process crash at a named failure point."""


class InjectedDrop(Exception):
    """Simulated lost message at a named failure point."""

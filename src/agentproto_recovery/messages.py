"""Small message / result types shared by components."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Code


@dataclass(frozen=True)
class AuthContext:
    """Principal and tenant as established by the (mock) authenticated
    transport, e.g. mTLS or a verified token. The prototype does not perform
    authentication; see docs/threat_model.md, assumption M1."""

    principal: str
    tenant: str
    authenticated: bool = True


@dataclass
class ResumeResult:
    code: Code
    epoch: int | None = None
    ticket: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code is Code.OK


@dataclass
class OpResult:
    code: Code
    status: str = ""  # COMMITTED | NOT_FOUND | PENDING | ...
    result: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code is Code.OK

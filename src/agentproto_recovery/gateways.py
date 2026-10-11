"""Gateways (mediators) G1, G2, ... operated by a gateway operator domain.

A gateway sees both the upstream (Domain A) and downstream (Domain B)
identifiers of every dialog it carries. That is inherent to its role and is
reported as such by the privacy evaluation; scoped identifiers only reduce
what the *endpoints* can link.

Configuration axes:
  id_scheme        "global"  - one dialog identifier end to end
                   "scoped"  - A and B each mint their own identifier
  recovery_source  "store"   - a replacement gateway must find the mapping
                               in a durable store it can read (shared with
                               the failed gateway: same operator)
                   "endpoint"- a replacement gateway may have no access to
                               the failed gateway's store and relies on what
                               the endpoint presents (the global ID, or an
                               opaque ticket sealed by Agent B)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .agents import AgentB, require_authz
from .authorization import AuthorizationService
from .correlation import CorrelationRecord, CorrelationStore
from .diagnostics import EventEmitter, ObservationLog
from .errors import (Code, InjectedCrash, InjectedDrop, RecoveryError, StoreUnavailable,
                     VersionConflict)
from .failure_injection import FailureInjector, ManualClock
from .lifecycle import State
from .messages import AuthContext, OpResult, ResumeResult
from .security import Redactor, TicketSealer, upstream_binding

T = TypeVar("T")
DOWNSTREAM_FAULTS = (InjectedDrop, InjectedCrash, StoreUnavailable)


@dataclass(frozen=True)
class GatewayConfig:
    id_scheme: str = "scoped"         # "global" | "scoped"
    recovery_source: str = "store"    # "store" | "endpoint"
    lease_seconds: float = 30.0
    # NEGATIVE CONTROL ONLY: guess a dialog for the same principal when the
    # mapping is missing. Exists to show the oracle detects wrong associations.
    unsafe_guess_on_missing: bool = False
    # Ticket sealed by the gateway with a key shared by cooperating gateway
    # operators (a pre-arranged mediator federation). Agent B never sees it.
    mediator_tickets: bool = False

    def __post_init__(self) -> None:
        assert self.id_scheme in ("global", "scoped")
        assert self.recovery_source in ("store", "endpoint")


class Gateway:
    def __init__(self, name: str, operator: str, cfg: GatewayConfig, store: CorrelationStore,
                 authz: AuthorizationService, agent_b: AgentB, injector: FailureInjector,
                 obs: ObservationLog, clock: ManualClock, log_key: bytes,
                 federation_key: bytes | None = None) -> None:
        self.name = name
        self.operator = operator
        self.cfg = cfg
        self.store = store
        self.authz = authz
        self.b = agent_b
        self.injector = injector
        self.obs = obs
        self.clock = clock
        self.alive = True
        self.fed_sealer = (TicketSealer(federation_key, "mediator-federation")
                           if cfg.mediator_tickets and federation_key else None)
        self.events = EventEmitter(f"gateway.{name}", operator, Redactor(log_key))

    # -- plumbing ------------------------------------------------------------
    def point(self, p: str) -> None:
        self.injector.check(f"gw.{self.name}.{p}")

    def hook(self, p: str) -> Callable[[], None]:
        return lambda: self.point(p)

    def _run(self, op: str, body: Callable[[], T], fail: Callable[[Code, str], T]) -> T:
        if not self.alive:
            return fail(Code.GATEWAY_DOWN, "not running")
        try:
            return body()
        except RecoveryError as e:
            self.events.emit(f"{op}_failed", code=e.code.value, level=logging.WARNING)
            return fail(e.code, e.detail)
        except StoreUnavailable as e:
            self.events.emit(f"{op}_failed", code=Code.STORE_UNAVAILABLE.value,
                             level=logging.WARNING)
            return fail(Code.STORE_UNAVAILABLE, str(e))
        except InjectedCrash as e:
            self.alive = False  # in-memory state is gone; durable store survives
            return fail(Code.GATEWAY_DOWN, f"crashed at {e}")
        except InjectedDrop as e:
            return fail(Code.DOWNSTREAM_UNAVAILABLE, f"dropped at {e}")

    def lookup(self, ctx: AuthContext, upstream_id: str) -> CorrelationRecord:
        if not ctx.authenticated:
            raise RecoveryError(Code.UNAUTHENTICATED)
        self.obs.saw(self.operator, "upstream_id", upstream_id)
        rec = self.store.get(ctx.tenant, upstream_id)
        if rec is None:
            raise RecoveryError(Code.MAPPING_NOT_FOUND)
        if rec.principal != ctx.principal:
            raise RecoveryError(Code.PRINCIPAL_MISMATCH)
        return rec

    def fed_seal(self, d_b: str, ctx: AuthContext, epoch: int, upstream_id: str) -> str:
        """Ticket for the mediator federation. Carries the upstream id so a
        replacement gateway can check the ticket belongs to the dialog being
        resumed; gateways already see that id, and A and B cannot open it."""
        assert self.fed_sealer is not None
        return self.fed_sealer.seal({"d": d_b, "u": upstream_id, "t": ctx.tenant,
                                     "p": ctx.principal, "e": epoch})

    def mark_interrupted(self, rec: CorrelationRecord) -> None:
        try:
            cur = self.store.get(rec.tenant, rec.upstream_id)
            if cur and cur.state in (State.ACTIVE.value, State.RESUMING.value):
                self.store.cas(cur, state=State.INTERRUPTED.value, owner=None, lease_until=None)
        except (VersionConflict, StoreUnavailable, RecoveryError):
            pass  # best effort; correctness never depends on this marker

    # -- operations ----------------------------------------------------------
    def establish(self, ctx: AuthContext, upstream_id: str, task: str) -> tuple[Code, str | None]:
        def body() -> tuple[Code, str | None]:
            if not ctx.authenticated:
                raise RecoveryError(Code.UNAUTHENTICATED)
            require_authz(self.authz, ctx, "delegate")
            self.obs.saw(self.operator, "upstream_id", upstream_id)
            self.point("before_forward")
            # Bind B's ticket (if any) to this upstream dialog without
            # revealing the upstream identifier to B.
            binding = upstream_binding(upstream_id) if self.b.use_tickets else None
            self.obs.saw(self.operator, "binding", binding)
            try:
                self.point("forward_establish")
                gid = upstream_id if self.cfg.id_scheme == "global" else None
                d_b, ticket = self.b.establish(ctx, task, global_id=gid, binding=binding)
            except DOWNSTREAM_FAULTS as e:
                raise RecoveryError(Code.DOWNSTREAM_UNAVAILABLE, str(e)) from e
            self.obs.saw(self.operator, "downstream_id", d_b)
            if self.fed_sealer is not None:
                ticket = self.fed_seal(d_b, ctx, 0, upstream_id)
            self.obs.saw(self.operator, "ticket", ticket)
            self.store.insert(
                CorrelationRecord(ctx.tenant, upstream_id, ctx.principal, d_b, self.b.name,
                                  State.ACTIVE.value, 0, 0),
                before_commit=self.hook("establish_before_commit"))
            self.point("establish_after_commit")
            self.events.emit("dialog_established", ids={"up": upstream_id, "down": d_b})
            return Code.OK, ticket
        return self._run("establish", body, lambda c, d: (c, None))

    def invoke(self, ctx: AuthContext, upstream_id: str, seq: int, payload: str) -> OpResult:
        def body() -> OpResult:
            rec = self.lookup(ctx, upstream_id)
            if rec.state == State.TERMINATED.value:
                raise RecoveryError(Code.DIALOG_TERMINATED)
            if rec.state != State.ACTIVE.value:
                raise RecoveryError(Code.INVALID_STATE, f"dialog is {rec.state}; resume first")
            self.point("before_forward")  # gateway-level crash point
            try:
                self.point("forward_invoke")  # message-loss point
                res = self.b.invoke(ctx, rec.downstream_id, seq, payload)
            except DOWNSTREAM_FAULTS as e:
                self.mark_interrupted(rec)
                raise RecoveryError(Code.DOWNSTREAM_UNAVAILABLE, str(e)) from e
            return res
        return self._run("invoke", body, lambda c, d: OpResult(c, detail=d))

    def op_status(self, ctx: AuthContext, upstream_id: str, seq: int) -> OpResult:
        def body() -> OpResult:
            rec = self.lookup(ctx, upstream_id)
            return self.b.op_status(ctx, rec.downstream_id, seq)
        return self._run("op_status", body, lambda c, d: OpResult(c, detail=d))

    def resume(self, ctx: AuthContext, upstream_id: str, epoch_seen: int, request_id: str,
               ticket: str | None) -> ResumeResult:
        from .recovery import gateway_resume
        return self._run("resume",
                         lambda: gateway_resume(self, ctx, upstream_id, epoch_seen, request_id,
                                                ticket),
                         lambda c, d: ResumeResult(c, detail=d))

    def terminate(self, ctx: AuthContext, upstream_id: str) -> Code:
        def body() -> Code:
            rec = self.lookup(ctx, upstream_id)
            try:
                self.b.terminate(ctx, rec.downstream_id)
            except DOWNSTREAM_FAULTS as e:
                raise RecoveryError(Code.DOWNSTREAM_UNAVAILABLE, str(e)) from e
            self.store.cas(rec, state=State.TERMINATED.value, owner=None, lease_until=None)
            return Code.OK
        return self._run("terminate", body, lambda c, d: c)

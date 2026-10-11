"""Gateway-side resumption procedure.

Checks run in this order; the first failure ends the attempt with an
explicit code (fail closed). No step falls back to matching on task content
or picking "the most likely" dialog (except the negative-control flag).

 1. authenticated context present                      -> UNAUTHENTICATED
 2. locate mapping by (tenant, upstream_id)
      store has it  -> verify HMAC tag                  -> MAPPING_INTEGRITY_FAILURE
      store lacks it, recovery_source == "store"        -> MAPPING_NOT_FOUND
      store lacks it, recovery_source == "endpoint":
          global id     -> forward the id to B
          scoped+B ticket        -> forward the B-sealed ticket to B
          scoped+mediator ticket -> open it with the federation key
          scoped only   -> MAPPING_NOT_FOUND
 3. principal binding                                   -> PRINCIPAL_MISMATCH
 4. lifecycle                                           -> DIALOG_TERMINATED
 5. authorization (independent of 2-4)                  -> AUTHZ_*
 6. idempotent retry (same request_id) returns cached result
 7. freshness: epoch presented == epoch stored          -> STALE_REFERENCE
 8. claim with CAS + lease                              -> CONCURRENT_RECOVERY
 9. forward to B; B repeats 3-7 against its own state   -> B's code
10. finalize with CAS
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .agents import require_authz
from .correlation import CorrelationRecord
from .errors import Code, RecoveryError, StoreUnavailable, VersionConflict
from .lifecycle import State
from .messages import AuthContext, ResumeResult
from .security import TicketInvalid

if TYPE_CHECKING:  # pragma: no cover
    from .gateways import Gateway


def _unsafe_guess(gw: Gateway, ctx: AuthContext) -> CorrelationRecord | None:
    rows = gw.store.db.query(
        "SELECT upstream_id FROM correlation WHERE tenant=? AND principal=? AND state!=? "
        "ORDER BY rowid DESC LIMIT 1", (ctx.tenant, ctx.principal, State.TERMINATED.value))
    return gw.store.get(ctx.tenant, rows[0]["upstream_id"]) if rows else None


def _release(gw: Gateway, claimed: CorrelationRecord, state: State) -> None:
    try:
        cur = gw.store.get(claimed.tenant, claimed.upstream_id)
        if cur is not None and cur.version == claimed.version:
            gw.store.cas(cur, state=state.value, owner=None, lease_until=None)
    except (VersionConflict, StoreUnavailable, RecoveryError):
        pass  # lease expiry covers this case


def gateway_resume(gw: Gateway, ctx: AuthContext, upstream_id: str, epoch_seen: int,
                   request_id: str, ticket: str | None) -> ResumeResult:
    from .gateways import DOWNSTREAM_FAULTS

    if not ctx.authenticated:
        raise RecoveryError(Code.UNAUTHENTICATED)
    gw.obs.saw(gw.operator, "upstream_id", upstream_id)
    gw.obs.saw(gw.operator, "ticket", ticket)

    rec = gw.store.get(ctx.tenant, upstream_id)            # step 2 (+ integrity)
    if rec is None and gw.cfg.unsafe_guess_on_missing:
        rec = _unsafe_guess(gw, ctx)
        if rec is not None:
            epoch_seen = rec.epoch  # a naive implementation trusts its guess
    claimed: CorrelationRecord | None = None
    ref: str | None
    tkt: str | None = None

    if rec is not None:
        if rec.principal != ctx.principal:                 # step 3
            raise RecoveryError(Code.PRINCIPAL_MISMATCH)
        if rec.state == State.TERMINATED.value:            # step 4
            raise RecoveryError(Code.DIALOG_TERMINATED)
        require_authz(gw.authz, ctx, "resume")             # step 5
        if rec.last_request_id == request_id and rec.last_result:   # step 6
            cached = json.loads(rec.last_result)
            return ResumeResult(Code.OK, cached["epoch"], cached["ticket"], "cached")
        if rec.epoch != epoch_seen:                        # step 7
            raise RecoveryError(Code.STALE_REFERENCE,
                                f"presented epoch {epoch_seen}, mapping at {rec.epoch}")
        now = gw.clock.now()                               # step 8
        if (rec.state == State.RESUMING.value and rec.owner != gw.name
                and (rec.lease_until or 0) > now):
            raise RecoveryError(Code.CONCURRENT_RECOVERY, "leased by another gateway")
        try:
            claimed = gw.store.cas(rec, state=State.RESUMING.value, owner=gw.name,
                                   lease_until=now + gw.cfg.lease_seconds,
                                   before_commit=gw.hook("claim_before_commit"))
        except VersionConflict as e:
            raise RecoveryError(Code.CONCURRENT_RECOVERY, str(e)) from e
        gw.point("after_claim")
        ref = claimed.downstream_id
    else:
        if gw.cfg.recovery_source != "endpoint":
            raise RecoveryError(Code.MAPPING_NOT_FOUND, "no durable mapping visible to this gateway")
        if gw.cfg.id_scheme == "global":
            ref = upstream_id
        elif ticket and gw.fed_sealer is not None:
            try:
                tp = gw.fed_sealer.open(ticket)
            except TicketInvalid as e:
                raise RecoveryError(Code.TICKET_INVALID, str(e)) from e
            if tp["t"] != ctx.tenant:
                raise RecoveryError(Code.TENANT_MISMATCH, "ticket")
            if tp["p"] != ctx.principal:
                raise RecoveryError(Code.PRINCIPAL_MISMATCH, "ticket")
            if tp["e"] != epoch_seen:
                raise RecoveryError(Code.STALE_REFERENCE, "ticket epoch")
            ref = tp["d"]
        elif ticket and gw.b.use_tickets:
            ref, tkt = None, ticket
        else:
            raise RecoveryError(Code.MAPPING_NOT_FOUND, "scoped id without mapping or ticket")
        require_authz(gw.authz, ctx, "resume")             # step 5

    try:                                                   # step 9
        gw.point("forward_resume")
        d_b, res = gw.b.resume(ctx, epoch_seen, ref=ref, ticket=tkt, mediator=gw.name)
    except DOWNSTREAM_FAULTS as e:
        if claimed:
            _release(gw, claimed, State.INTERRUPTED)
        raise RecoveryError(Code.DOWNSTREAM_UNAVAILABLE, str(e)) from e
    except RecoveryError as e:
        if claimed:
            _release(gw, claimed, State.TERMINATED if e.code is Code.DIALOG_TERMINATED
                     else State.INTERRUPTED)
        raise

    gw.obs.saw(gw.operator, "downstream_id", d_b)
    if gw.fed_sealer is not None:
        res = ResumeResult(Code.OK, res.epoch, gw.fed_seal(d_b, ctx, res.epoch))
    gw.obs.saw(gw.operator, "ticket", res.ticket)
    gw.point("before_finalize")
    result_json = json.dumps({"epoch": res.epoch, "ticket": res.ticket})
    if claimed is not None:                                # step 10
        gw.store.cas(claimed, state=State.ACTIVE.value, epoch=res.epoch, owner=None,
                     lease_until=None, last_request_id=request_id, last_result=result_json,
                     before_commit=gw.hook("finalize_before_commit"))
    else:
        existing = gw.store.get(ctx.tenant, upstream_id)
        if existing is None:
            gw.store.insert(
                CorrelationRecord(ctx.tenant, upstream_id, ctx.principal, d_b, gw.b.name,
                                  State.ACTIVE.value, res.epoch, 0, None, None,
                                  request_id, result_json),
                before_commit=gw.hook("finalize_before_commit"))
        else:
            gw.store.cas(existing, state=State.ACTIVE.value, epoch=res.epoch, owner=None,
                         lease_until=None, last_request_id=request_id,
                         last_result=result_json)
    gw.events.emit("dialog_resumed", ids={"up": upstream_id, "down": d_b}, epoch=res.epoch)
    return res

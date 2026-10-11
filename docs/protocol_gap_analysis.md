# Protocol gap analysis

## Sources reviewed (2026-10-11)

| Source | State when reviewed |
|---|---|
| draft-agentic-ai-usecases-requirements-03 (Reddy.K, Sarker, Yao) | Individual Internet-Draft, intended Informational, dated 10 Oct 2026. Not a WG document. |
| AgentProto charter (charter-ietf-agentproto-01) | WG active, ART area. Milestone: adopt use-cases draft Dec 2026, protocol drafts Mar 2027. |
| github.com/tireddy2/agentic-ai-ucreq2 issue #27 "Correlation identifier" | Open. A one-line question (by Z. Sarker) on whether correlation identifiers are needed to relate subtasks to a task. No visible comments. |
| github.com/tireddy2/agentic-ai-ucreq2 PR #72 "Updated draft to align with charter" | Open, opened 8 Oct 2026. Rewords B3-13 toward associating each downstream interaction with the upstream interaction that caused it. Review comment on B3-13 questions whether this needs to be in the protocol, since participants can map identifiers carried in context themselves. |

Not verified by me: the AgentProto mailing-list thread in which the project
author raised "Privacy-Preserving Dialog Resumption Across Administrative
Trust Boundaries" and the reply that most general requirements are already
addressed. That context is taken from the project brief.

Requirement text was read from the -03 text. It is paraphrased below with
requirement IDs; consult the draft for exact wording. Because the draft and
PR #72 are moving, any mapping here is tied to revision -03.

## Requirement mapping

"Stated" = what the draft says. "Interpretation" = how this project reads it;
those readings are mine, not the authors'.

| Req. | Stated in -03 (paraphrase) | What the prototype exercises | Interpretation / note |
|---|---|---|---|
| B1-6 | Persistent dialog identifiers that survive interruption or network change; a resumption message to re-attach; restore prior dialog context | Resume message carrying `(dialog ref, epoch, request_id[, ticket])`; T03, T04 | The draft does not say whether "persistent dialog identifier" means one value end to end or a per-hop / per-domain value. The prototype tests both. |
| B1-7 | On resumption, receiver can verify the resuming party is the interrupted participant and that the dialog is not terminated and authorization not revoked; otherwise not resumed and failure signalled | Principal binding at gateway and B, terminated check, separate authz, explicit codes; T09-T11, T13, T19 | Directly covered. The draft names identity, termination and revocation; it does not name *freshness* (stale or replayed resumption). The prototype needed an epoch for T08/T12b. |
| B3-13 | (-03) Propagate dialog context across each hop of a delegation chain so participants can correlate along the chain. (PR #72) associate each downstream interaction with its upstream cause | Gateway mapping dA→dB per hop | Satisfied by per-hop mapping held by the hop that does the mapping. Supports the PR #72 reviewer's view that mapping can be local — *as long as that hop or its state survives*. |
| B5-2 | Continue a dialog through a different mediator when the current mediator fails | T04, T05, T16, all six modes | Covered as an outcome. The draft does not say what the replacement mediator must know, or whether it may be operated by a different party. This is where the modes diverge. |
| B5-3 | A translating mediator carries dialog context across the translation so each side can correlate | Not separately modelled (no protocol translation) | Per-hop identifiers are compatible with this wording. |
| CMN-11 | Establish, modify, terminate; dialog lifetime | Establish / terminate; no lifetime | Lifetime would clean up the orphan B-side dialogs seen in T16b. Not implemented. |
| Sec. 7 | Persistent identifiers across dialogs and domains enable tracking; designers should consider pseudonymous or temporary identifiers, balanced against audit and accountability | T17 privacy metrics | Covered as a consideration. The experiment quantifies some of the trade-off; it does not resolve it. |
| — | No requirement on idempotency, duplicate execution after resumption, or mediator state transfer (checked in -03) | T14, T14b | The draft says task context is not carried by the protocol, and the charter excludes tool-specific logic. See finding F6. |

## Findings and their cause

Cause categories (from the brief): (a) protocol-level information not
available, (b) missing persistence, (c) local implementation mistake,
(d) authorization policy, (e) security assumption, (f) limits of the
simulated environment.

**F1. Scoped identifiers with a replacement gateway that can read the failed
gateway's durable store recover correctly.** `scoped-store` recovered in every
case where state was intact (T04, T14, T16) and failed closed otherwise.
It returned the same code as `global-store` in all 22 scenarios; the only
behavioural difference was T07, where the second tenant's establishment
succeeded under scoped ids and was refused under global ids. Cause of failures when they occur: (b) missing persistence
(T05, T16b) or (d) policy. **Classification: solvable by local implementation
(2); B5-2 already states the outcome (1).**

**F2. Scoped identifiers with an independently operated replacement gateway
cannot recover unless something the endpoint presents lets the new gateway
find the downstream dialog.** `scoped-endpoint-noticket` recovered 0 of 270
attempts in the fault schedule and failed every recoverable scenario with
`MAPPING_NOT_FOUND`. Three things fixed it in this prototype:

* a global identifier (`global-endpoint`) — every domain then holds the same
  value (A~B identifier join 1.0, 4 of 4 domains share one value);
* an opaque ticket sealed by B and carried by A (`scoped-endpoint-ticket`) —
  works, but the ticket bytes are a value both A and B hold, so colluding
  endpoints can join (A~B join 1.0). Unlike a global id, it changes every
  resumption (2 distinct shared values per dialog over one resumption);
* an opaque ticket sealed by G1 for a mediator federation
  (`scoped-endpoint-medticket`) — A~B join 0.0, but it requires mediator
  operators to pre-share a key, and both operators can join their logs
  (GW1~GW2 join 1.0).

The missing piece in the noticket case is information that has to travel in
a protocol message (resumption material that a different operator can use),
so the cause is (a). Whether a protocol needs to define such material, and
whether the dialog identifier is end-to-end or per hop, is not settled by
B1-6 or B5-2. **Classification: a protocol-design question (4), not evidence
for a new use-case requirement.** The requirements already say the dialog
must continue through a different mediator; they do not need to pick a
mechanism. The protocol and architecture documents will have to.

**F3. No configuration tested prevents linkage by everyone.** Gateways always
link A-side and B-side identifiers (1.0 in every mode where they forward).
Event order alone linked A and B in 100% of dialogs in every mode in the
sequential workload. Identifier scoping only removes the *identifier-equality*
join between endpoints (scoped-store, medticket). Cause: (e) inherent to the
trust model plus (f) the sequential workload. **Classification: already
covered by Security Considerations (1); the numbers can inform that text.**

**F4. Global identifiers chosen by the initiator collide at a receiver that
keys dialogs by the identifier alone.** T07: the second tenant's
establishment was refused (`INVALID_STATE`). That is fail-closed, but it is a
squatting surface. Cause: (c) local implementation choice — keying B's table
by `(tenant, id)` removes it. **Classification: local (2).**

**F5. Freshness had to be carried end to end.** Without an epoch in the
resumption message, B could not have rejected T08 (old ticket) or the T12b
rollback once the gateway's own state was rolled back. B1-7 lists identity,
termination and revocation; it does not list staleness. This was not tested
by ablation (no run without the epoch), so it is an observation, not a
demonstrated failure. Cause: (a). **Classification: possibly worth a short
clarification in B1-7 (4); many protocol designs include sequence numbers
anyway, so it may be considered implied.**

**F6. Duplicate side effects after resumption were avoided entirely at the
endpoint.** Per-dialog sequence numbers plus a write-ahead `PENDING` row at B
gave exactly one execution in T14/T14b; an unknown outcome was surfaced as
`OUTCOME_UNKNOWN` and resolved by reconciliation. The only protocol-visible
part is a per-dialog operation number and a way to ask its status after
resumption. Using random per-operation ids instead would add a cross-domain
join key. Cause when it goes wrong: (c). **Classification: local (2), with a
minor protocol-design note (the request numbering is protocol-visible).**

**F7. A misbehaving gateway that guesses can mis-associate dialogs, and B
cannot detect it when the guess stays within the same principal.** The
negative control produced 23 incorrect associations in 270 attempts. B's
principal/tenant/epoch checks did not catch them because the guessed dialog
belonged to the same principal. Cause: (e) the gateway is trusted for
association. A defence would need the resuming party to prove knowledge of
something specific to the dialog that the gateway cannot substitute (the
B-sealed ticket does this; in `scoped-endpoint-ticket` the gateway has no
mapping to guess from). **Classification: security consideration (4); not
tested beyond the negative control.**

**F8. Crash during establishment leaves an orphan dialog at B (T16b).** Fail
closed for A, but B holds state nobody can resume except in `global-endpoint`.
Cause: (b). Dialog lifetime (CMN-11) would clean it up. **Classification:
covered (1).**

**F9. Independently operated replacement gateways cannot see each other's
claims.** In T18 both succeeded in endpoint modes; B advanced exactly once
and both got the same epoch, so no association error or double transition
occurred. Cause: by design. **Classification: local (2).**

## Conclusion

The experiment does **not** show a missing use-case requirement. The
outcomes the brief cares about — resumption after mediator failure,
verification of the resuming party and its authorization, failure signalling,
and awareness of identifier linkability — are stated in -03 (B1-6, B1-7, B5-2,
Section 7).

It does show one place where the requirements leave a design question open
that determines whether the privacy goal is reachable: **when the replacement
mediator is operated by a different party than the failed one, what recovery
material does it get, and from whom?** In this prototype the only working
answers were (i) a common identifier visible to every domain, (ii) an opaque
handle that both endpoints hold, or (iii) a handle opened by a pre-arranged
federation of mediators. Each moves the linkability somewhere else; none
removes it. If the same operator runs both mediators (shared durable state),
the question disappears and scoped identifiers work with purely local
mechanisms.

So the answer to the brief's four options is: mostly (1) and (2); the
independently-operated-mediator case is (4), a protocol-design question for
the protocol and architecture work rather than a new requirement for the
use-cases draft. Suggested wording is in `ietf_contribution_notes.md` and is
offered only as an optional clarification.

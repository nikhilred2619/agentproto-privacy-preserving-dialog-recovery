# Protocol gap analysis

## Sources reviewed (2026-10-11)

| Source | State when reviewed |
|---|---|
| draft-agentic-ai-usecases-requirements-03 (Reddy.K, Sarker, Yao) | Individual Internet-Draft, intended Informational, dated 10 Oct 2026. Not a WG document. |
| AgentProto charter (charter-ietf-agentproto-01) | WG active, ART area. Milestone: adopt use-cases draft Dec 2026, protocol drafts Mar 2027. |
| github.com/tireddy2/agentic-ai-ucreq2 issue #27 "Correlation identifier" | Open. A one-line question (by Z. Sarker) on whether correlation identifiers are needed to relate subtasks to a task. No visible comments. |
| github.com/tireddy2/agentic-ai-ucreq2 PR #72 "Updated draft to align with charter" | **Merged 9 Oct 2026** (merge commit `f9920d8`, 18:02 +0530), opened 8 Oct. It *introduced* B3-13. The first commit (`29561c9`) worded it as associating each downstream interaction with the upstream interaction that caused it; commit `3c7406a` changed it to the "propagate the dialog context across each hop" wording that was merged and appears in -03. The PR page showed a review comment questioning whether B3-13 needs to be in the protocol, since participants can map identifiers carried in context themselves (comment text not re-verified after the merge). |

Correction (v0.2.0): the v0.1.0 version of this document said PR #72 was
still open on 11 Oct and that it "rewords" B3-13. Both were wrong. The merge
date and the wording history above were checked on 11 Oct 2026 from the
repository's git history (`git log origin/main`, `git show <commit>:draft-…md`);
a web rendering of the PR page fetched the same day still displayed "Open",
which is how the error arose.

Not verified by me: the AgentProto mailing-list thread in which the project
author raised "Privacy-Preserving Dialog Resumption Across Administrative
Trust Boundaries" and the reply that most general requirements are already
addressed. That context is taken from the project brief.

Requirement text was read from the -03 text and checked against the
repository's `main` after the PR #72 merge (identical for every requirement
mapped below). It is paraphrased with requirement IDs; consult the draft for
exact wording. The mapping is tied to revision -03.

## Requirement mapping

"Stated" = what the draft says. "Interpretation" = how this project reads it;
those readings are mine, not the authors'.

| Req. | Stated in -03 (paraphrase) | What the prototype exercises | Interpretation / note |
|---|---|---|---|
| B1-6 | Persistent dialog identifiers that survive interruption or network change; a resumption message to re-attach; restore prior dialog context | Resume message carrying `(dialog ref, epoch, request_id[, ticket])`; T03, T04 | The draft does not say whether "persistent dialog identifier" means one value end to end or a per-hop / per-domain value. The prototype tests both. |
| B1-7 | On resumption, receiver can verify the resuming party is the interrupted participant and that the dialog is not terminated and authorization not revoked; otherwise not resumed and failure signalled | Principal binding at gateway and B, terminated check, separate authz, explicit codes; T09-T11, T13, T19 | Directly covered. The draft names identity, termination and revocation; it does not name *freshness* (stale or replayed resumption). The prototype needed an epoch for T08/T12b. |
| B3-13 | Propagate dialog context across each hop of a delegation chain so participants can correlate along the chain (added by PR #72, merged 9 Oct; an interim wording in the PR spoke of associating each downstream interaction with its upstream cause) | Gateway mapping dA→dB per hop | Satisfied by per-hop mapping held by the hop that does the mapping. Consistent with the review comment on the PR that mapping can be local — *as long as that hop or its state survives*. |
| B5-2 | Continue a dialog through a different mediator when the current mediator fails | T04, T05, T16, all six modes | Covered as an outcome. The draft does not say what the replacement mediator must know, or whether it may be operated by a different party. This is where the modes diverge. |
| B5-3 | A translating mediator carries dialog context across the translation so each side can correlate | Not separately modelled (no protocol translation) | Per-hop identifiers are compatible with this wording. |
| CMN-11 | Establish, modify, terminate; dialog lifetime | Establish / terminate; no lifetime | Lifetime would clean up the orphan B-side dialogs seen in T16b. Not implemented. |
| Sec. 7 | Persistent identifiers across dialogs and domains enable tracking; designers should consider pseudonymous or temporary identifiers, balanced against audit and accountability | T17 privacy metrics | Covered as a consideration. The experiment quantifies some of the trade-off; it does not resolve it. |
| — | No requirement on idempotency, duplicate execution after resumption, or mediator state transfer (checked in -03) | T14, T14b, T22, T23 | The draft says task context is not carried by the protocol, and the charter excludes tool-specific logic. See finding F6. |

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
  works once the ticket is bound to the upstream dialog (see F10), but the ticket bytes are a value both A and B hold, so colluding
  endpoints can join (A~B join 1.0). Unlike a global id, it changes every
  resumption (2 distinct shared values per dialog over one resumption);
* an opaque ticket sealed by G1 for a mediator federation
  (`scoped-endpoint-medticket`), also bound to the upstream dialog — A~B join
  0.0, but it requires mediator
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

**F6. Duplicate side effects after resumption can be avoided entirely at the
endpoint — v0.1.0 got two parts of it wrong.** Per-dialog sequence numbers
plus a write-ahead `PENDING` row at B gave exactly one execution in
T14/T14b. The audit found two local mistakes: reconciliation could clear a
`PENDING` row while the tool call was still running, which led to a second
execution; and a reused sequence number with a different payload was treated
as a duplicate. v0.2.0 adds an execution lease and incarnation id, fencing at
the tool, and a payload digest (T22, T23: one execution each). The only
protocol-visible parts are a per-dialog operation number, a way to ask its
status after resumption, and a rule that one number names one operation.
Using random per-operation ids instead would add a cross-domain join key.
Safe reconciliation also needs the external system to offer fencing or
idempotency keys, which is outside any dialog protocol. Cause of both
defects: (c) local implementation mistakes. **Classification: local (2).
Not an interoperability gap.**

**F7. A misbehaving gateway that guesses can mis-associate dialogs, and B
cannot detect it when the guess stays within the same principal.** The
negative control produced 13 incorrect associations in 270 attempts (v0.2.0
run; 23 in the v0.1.0 run, whose fault draws differed). B's
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

**F10. Recovery material must be bound to the dialog it is presented for.**
In v0.1.0 an authentic, fresh ticket for dialog 2 resumed dialog 1 when
presented with dialog 1's identifier (both ticket modes), and the replacement
gateway then stored that wrong association. Integrity, freshness, principal
binding and authorization all passed; what was missing was a check that the
ticket belongs to the dialog named in the request. v0.2.0 binds each ticket
to the upstream dialog (dA inside mediator tickets; `upstream_binding(dA)`,
an HMAC keyed by dA, inside B tickets, so B does not learn dA). After the
fix: T21 `TICKET_MISMATCH` in both modes and 0 incorrect associations under
the new `foreign_ticket` fault. Cause: (c) local design mistake, but the
lesson carries over to any protocol that lets an opaque resumption handle
travel next to a dialog reference: whoever interprets the handle must be able
to check it against that reference, and with per-domain identifiers the check
needs a value derived from the reference rather than the reference itself if
the interpreter is in another domain. **Classification: (4) a protocol-design
point for whoever specifies resumption material, not a new use-case
requirement; B1-7 already requires verifying that the resuming party is the
one whose exchange was interrupted, which the v0.1.0 ticket path did not
fully do.**

## Conclusion

The experiment does **not** show a missing use-case requirement. The
outcomes the brief cares about — resumption after mediator failure,
verification of the resuming party and its authorization, failure signalling,
and awareness of identifier linkability — are stated in -03 (B1-6, B1-7, B5-2,
Section 7).

It does show one place where the requirements leave a design question open
that determines whether the privacy goal is reachable: **when the replacement
mediator is operated by a different party than the failed one, what recovery
material does it get, from whom, and how is it tied to the dialog being
resumed?** In this prototype the only working answers were (i) a common identifier visible to every domain, (ii) an opaque
handle that both endpoints hold, or (iii) a handle opened by a pre-arranged
federation of mediators. Each moves the linkability somewhere else; none
removes it. If the same operator runs both mediators (shared durable state),
the question disappears and scoped identifiers work with purely local
mechanisms.

The audit findings fixed in v0.2.0 do not change this conclusion. Two of
them (F6) were local implementation mistakes. The third (F10) was a local
design mistake whose general form — recovery material must be checkable
against the dialog it is presented for — belongs with the same open design
question, not with a new requirement.

So the answer to the brief's four options is: mostly (1) and (2); the
independently-operated-mediator case is (4), a protocol-design question for
the protocol and architecture work rather than a new requirement for the
use-cases draft. Suggested wording is in `ietf_contribution_notes.md` and is
offered only as an optional clarification.

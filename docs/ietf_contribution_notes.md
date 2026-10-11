# Draft technical note for AgentProto review

*Status: draft for the author's review. Not sent anywhere. Independent
experiment; not an IETF document and not a claim of compliance with any
draft.*

---

**Subject:** Experiment report: dialog resumption across a mediator failure
with per-domain dialog identifiers (re: B1-6, B1-7, B5-2, Sec. 7 of
draft-agentic-ai-usecases-requirements-03)

**Problem statement.** Agent A (domain A) delegates to Agent B (domain B)
through mediator G1. G1 fails and the dialog continues through mediator G2.
Can this work if A and B use different dialog identifiers, so that no single
identifier is visible to both endpoints? And what does G2 need to know?

**Requirements examined.** B1-6 (persistent identifiers, resumption message),
B1-7 (verify resuming party, termination, revocation; signal failure), B3-13
(correlation across delegation hops; current text and the rewording in PR #72
of the editors' repository), B5-2 (continuation through a different
mediator), B5-3 (context across translation, not modelled), CMN-11, and
Section 7 (persistent identifiers and linkability).

**Setup.** A single-process Python prototype with SQLite state per domain,
a mock authorization service, and deterministic failure injection. Six
configurations: global vs. per-domain identifiers; replacement mediator run
by the same operator (reads the failed mediator's durable store) vs. by a
different operator (no access to it); and, for the latter, nothing / an
opaque ticket sealed by B / an opaque ticket sealed by a pre-arranged
federation of mediators. 22 scripted scenarios, a seeded fault schedule
(1,620 recovery attempts over 6 configurations), an identifier-linkability
analysis, and latency measurements. Trust assumptions are listed in
`docs/threat_model.md` (authentication and key custody are mocked).

**Minimal failing scenario.** Per-domain identifiers; G1 crashes; G2 is
operated by a different party and cannot read G1's state; A presents only its
own identifier. G2 has no way to find B's dialog. Result: explicit
`MAPPING_NOT_FOUND`, 0 of 270 recoveries. Reproduce with
`pytest tests/test_gateway_failover.py -k "t04 and noticket"`.

**Evidence.**

1. With the same operator running G1 and G2 (shared durable mapping),
   per-domain identifiers recovered in every case a global identifier did,
   failed closed in the same cases, and gave a 0.0 endpoint-to-endpoint
   identifier join (global: 1.0). This needs only local mechanisms.
2. With independently operated mediators, recovery worked only when some
   recovery material travelled with the resumption request: a global
   identifier (all four domains share one value), a B-sealed ticket (both
   endpoints hold the ticket bytes, so colluding endpoints can still join, but
   the value rotates per resumption), or a mediator-federation ticket
   (endpoints share nothing, but mediator operators must pre-share a key and
   can join their own logs).
3. In every configuration, mediators linked both sides of every dialog they
   carried, and event order alone linked A and B in all dialogs of a
   sequential workload.
4. Across all configurations: no resumption attached to the wrong dialog,
   and none succeeded when authorization was expired or revoked, the dialog
   was terminated, the reference was stale, or another principal presented
   it. A deliberately guessing mediator (negative control) produced 23 wrong
   attachments in 270 attempts; the receiving agent could not detect guesses
   that stayed within the same principal.
5. The prototype needed a per-dialog epoch in the resumption message to
   reject stale or rolled-back references. B1-7 names identity, termination
   and revocation but not staleness. (Not tested by ablation.)
6. Duplicate side effects after a lost acknowledgment were avoided with a
   per-dialog operation number and receiver-side bookkeeping. The draft has no
   requirement here and the charter excludes tool-specific logic; this looks
   like an implementation matter.

**Does this need standardization?** The evidence does not support a new
use-case requirement. B1-6, B1-7, B5-2 and Section 7 already state the needed
outcomes. It does point to a design question the requirements leave open and
that the protocol/architecture work will have to answer: *whether the dialog
identifier is end-to-end or per hop, and, when a replacement mediator is run
by a different party, what recovery material it receives and from whom.*
Every working answer in this experiment moved linkability to a different
party rather than removing it.

**Optional clarifications (for discussion only).** If the editors think
it useful to make the open question visible in the requirements document,
two possible sentences:

* B5-2, appended: "The replacement mediator might not be operated by the same
  party as the failed mediator and might not have access to its state."
* B1-7, appended: "The receiving participant is also able to detect that a
  resumption request refers to an outdated state of the dialog."

Neither is needed for the use cases to be understood; both are offered as
editorial input.

**Known limitations.** In-process simulation, no network transport, no
multi-host concurrency, mocked authentication and authorization, a single
clock, one linkage attack beyond identifier equality, sequential workload for
that attack, no protocol translation, and no formal privacy property.

**Code and data.** `<repository URL — to be added when published>`;
`README.md` lists the commands; raw results are in `results/`.

---

## Suggested next steps for external review

1. Read `docs/protocol_gap_analysis.md` and check that the requirement
   paraphrases still match the latest revision (PR #72 was open on
   2026-10-11 and changes B3-13).
2. Publish the repository (your decision) so the note can link to it.
3. Ask one or two engineers outside the project to run `pytest` and
   `agentproto-recovery all` on a clean machine and report differences.
4. Consider whether to post a short version of this note to the AgentProto
   list, or as a comment on issue #27 / PR #72 in the editors' repository,
   where the B3-13 discussion (local mapping vs. protocol) is directly
   related to finding 1.
5. If people respond, the most useful follow-up experiments would be the
   epoch ablation, a concurrent-establishment version of the order attack, and
   a real HTTP binding.

# OASB issue #150 — preliminary detection-fixture design

**Status: independent preparatory work. NOT reviewed, merged, endorsed, or integrated with OASB.**

Tracked proposal: https://github.com/opena2a-org/oasb/issues/150
Existing AgentProto research: https://github.com/nikhilred2619/agentproto-privacy-preserving-dialog-recovery

## Proposed benchmark question

Can an AI-agent security product detect a valid, same-principal, same-epoch recovery ticket being used for a *different* dialog after gateway failover, while leaving legitimate recovery unflagged?

The existing AgentProto research has real ticket-validation regression tests in `tests/test_audit_regressions.py::test_ticket_of_other_dialog_is_rejected`. These new fixtures do NOT implement that protocol. Instead, they explore whether detection is possible when trusted ticket-issuance and recovery observations are available to a security product.

## Run locally

Requires Node.js 18+; no dependencies or network access:

```sh
node --test contrib/oasb-150/scenario.test.mjs
```

These tests validate synthetic fixtures using a **toy reference correlation oracle**. They do NOT establish an OASB benchmark score or demonstrate that any security product detects this attack. The OASB harness was not run.

## Proposed observation contract (subject to maintainer review)

- Trusted `ticket-issued` observation: opaque ticket fingerprint, dialog reference, tenant, principal and epoch. This event MUST originate from a trusted issuer or verifier, never attacker-controlled data.
- `dialog-resume-attempt` observation: requested dialog reference, opaque ticket fingerprint, tenant, principal, epoch, and gateway ID.
- The detector must correlate both observations; changing gateway IDs during failover is legitimate.
- Never send full ticket bytes, credentials, ground-truth labels or expected verdicts to the product under test.
- Absent trusted issuance evidence or a suitable adapter capability, report N/A or inconclusive rather than a false negative.

The current OASB `A2AScanner.scanMessage(from, to, content)` accepts only sender, recipient, and message text; it does not expose structured recovery or ticket-binding metadata. An adapter extension or alternative trusted event hook must be discussed before adding an OASB test. Pre-injecting an event already labeled `threat` would leak the answer and invalidate a detection claim.

## Limits

All fixtures and fingerprints are synthetic. The reference checker cannot validate ticket cryptography, distinguish forged issuance events, model live failover, measure real-world false-positive rates, or prevent duplicate downstream tool execution. Real AgentProto regression tests are separate from security-product detection benchmarks.

## OASB integration checklist (after issue review)

1. Obtain maintainer agreement on where trusted observations are produced and which OASB adapter capability applies.
2. Define a reproducible product-input contract, a capability gate, and independent hidden ground-truth labels.
3. Review OWASP Agentic ASI07, potential LLM mappings, and any MITRE ATLAS mapping with maintainers; do not invent a technique ID.
4. Port scenario fixtures into the agreed OASB harness with positive and negative controls and no ground-truth leakage.
5. From a clean upstream checkout, run `npm run test:scripts`, `npm test`, and `npm run build`, and update all required mapping, documentation, and scenario counts.
6. Submit a PR only after the design is agreed and tests have been run, documenting any failures without overstating results.

Co-authorship or maintainer acceptance is not presumed. Credit is subject to upstream review.

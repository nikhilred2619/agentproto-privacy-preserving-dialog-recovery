/**
 * Lab-only reference correlation oracle; NOT an OASB SecurityProductAdapter.
 * It correlates trusted synthetic observations; it DOES NOT validate
 * cryptographic tickets or demonstrate real-product threat detection.
 */
export function evaluateObservations(observations) {
  const issued = new Map();
  const verdicts = [];
  for (const event of observations) {
    if (event.type === 'ticket-issued') {
      const prior = issued.get(event.ticketFingerprint);
      const binding = {
        dialogRef: event.dialogRef, tenant: event.tenant,
        principal: event.principal, epoch: event.epoch,
      };
      if (prior && JSON.stringify(prior) !== JSON.stringify(binding)) {
        issued.set(event.ticketFingerprint, null); // conflicting evidence
      } else if (!issued.has(event.ticketFingerprint)) {
        issued.set(event.ticketFingerprint, binding);
      }
    } else if (event.type === 'dialog-resume-attempt') {
      const bound = issued.get(event.ticketFingerprint);
      if (!bound) {
        verdicts.push({ detected: null, status: 'insufficient-context' });
        continue;
      }
      const mismatch = ['dialogRef', 'tenant', 'principal', 'epoch']
        .some(field => bound[field] !== event[field]);
      verdicts.push({
        detected: mismatch,
        status: mismatch ? 'binding-mismatch' : 'binding-match',
      });
    } else {
      throw new Error('Unknown event type: ' + event.type);
    }
  }
  return verdicts;
}

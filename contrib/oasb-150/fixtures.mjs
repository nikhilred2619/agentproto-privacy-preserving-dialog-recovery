/**
 * Synthetic laboratory fixtures for OASB issue #150.
 * Ground truth is outside the event stream delivered to a product.
 */
const ISSUANCE_A = Object.freeze({
  type: 'ticket-issued', tenant: 'tenant-x', principal: 'principal-1',
  dialogRef: 'dialog-A', epoch: 0,
  ticketFingerprint: 'fixture-fingerprint-A', gatewayId: 'gateway-G1',
});
const ISSUANCE_B = Object.freeze({
  type: 'ticket-issued', tenant: 'tenant-x', principal: 'principal-1',
  dialogRef: 'dialog-B', epoch: 0,
  ticketFingerprint: 'fixture-fingerprint-B', gatewayId: 'gateway-G1',
});

export function recovery(dialogRef, ticketFingerprint, gatewayId = 'gateway-G2') {
  return {
    type: 'dialog-resume-attempt', tenant: 'tenant-x', principal: 'principal-1',
    dialogRef, epoch: 0, ticketFingerprint, gatewayId,
  };
}

// Both tickets are assumed authentic for the same tenant, principal and epoch.
// Only dialog binding differs; no live secret or real ticket bytes are used.
export const scenarios = Object.freeze([
  {
    id: 'OASB-150-BENIGN-A', groundTruth: 'benign',
    reason: 'Dialog A legitimately resumes after gateway failover.',
    observations: [ISSUANCE_A, ISSUANCE_B, recovery('dialog-A', 'fixture-fingerprint-A')],
  },
  {
    id: 'OASB-150-BENIGN-B', groundTruth: 'benign',
    reason: 'Dialog B legitimately resumes after gateway failover.',
    observations: [ISSUANCE_A, ISSUANCE_B, recovery('dialog-B', 'fixture-fingerprint-B')],
  },
  {
    id: 'OASB-150-ATTACK-B-INTO-A', groundTruth: 'malicious',
    reason: 'Ticket B is used to resume Dialog A.',
    observations: [ISSUANCE_A, ISSUANCE_B, recovery('dialog-A', 'fixture-fingerprint-B')],
  },
  {
    id: 'OASB-150-ATTACK-A-INTO-B', groundTruth: 'malicious',
    reason: 'Ticket A is used to resume Dialog B.',
    observations: [ISSUANCE_A, ISSUANCE_B, recovery('dialog-B', 'fixture-fingerprint-A')],
  },
]);

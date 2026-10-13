import test from 'node:test';
import assert from 'node:assert/strict';
import { scenarios, recovery } from './fixtures.mjs';
import { evaluateObservations } from './reference-detector.mjs';

test('fixture set includes positive and negative controls', () => {
  assert.equal(scenarios.filter(s => s.groundTruth === 'benign').length, 2);
  assert.equal(scenarios.filter(s => s.groundTruth === 'malicious').length, 2);
});

for (const scenario of scenarios) {
  test(scenario.id + ': reference correlation matches the independent ground truth', () => {
    const result = evaluateObservations(scenario.observations);
    assert.equal(result.length, 1);
    assert.equal(result[0].detected, scenario.groundTruth === 'malicious');
    assert.notEqual(result[0].status, 'insufficient-context');
  });
  test(scenario.id + ': no answer key or full ticket in product-visible observations', () => {
    for (const event of scenario.observations) {
      for (const forbidden of ['groundTruth', 'expectedDetected', 'verdict', 'ticket', 'ticketBytes']) {
        assert.equal(forbidden in event, false);
      }
    }
  });
}

test('without trusted issuance, detection is N/A, not a false negative', () => {
  assert.deepEqual(
    evaluateObservations([recovery('dialog-A', 'fixture-fingerprint-B')]),
    [{ detected: null, status: 'insufficient-context' }],
  );
});

test('conflicting issuance observations give an inconclusive result', () => {
  const events = [
    { type: 'ticket-issued', tenant: 'tenant-x', principal: 'principal-1',
      dialogRef: 'dialog-A', epoch: 0, ticketFingerprint: 'f' },
    { type: 'ticket-issued', tenant: 'tenant-x', principal: 'principal-1',
      dialogRef: 'dialog-B', epoch: 0, ticketFingerprint: 'f' },
    recovery('dialog-A', 'f'),
  ];
  assert.deepEqual(evaluateObservations(events), [
    { detected: null, status: 'insufficient-context' },
  ]);
});

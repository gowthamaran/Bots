import test from 'node:test';
import assert from 'node:assert/strict';
import { orderScore, computeMakerScores } from '../src/scoring.js';

test('docs worked example core values', () => {
  const v = 3;
  assert.equal(orderScore(v, 1, 1), 4/9);
  const result = computeMakerScores({
    v, c: 3, midpoint: 0.5,
    book: {
      mBids: [{ spreadCents: 1, size: 400 }],
      mAsks: [{ spreadCents: 2, size: 200 }],
      mPrimeBids: [{ spreadCents: 2, size: 200 }],
      mPrimeAsks: [{ spreadCents: 1, size: 400 }]
    }
  });
  assert.ok(result.qOne > 0);
  assert.ok(result.qTwo > 0);
});

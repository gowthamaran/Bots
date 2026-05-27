import { projectMarketReward } from '../src/compute.js';

const market = {
  id: process.argv[2] ?? 'demo',
  rewardPoolPerDay: Number(process.argv[3] ?? 1000),
  maxIncentiveSpreadCents: 3,
  c: 3,
  snapshot: { midpoint: 0.5, yes: { bids: [{ price: 0.49, size: 1000 }], asks: [{ price: 0.51, size: 1000 }] }, no: { bids: [{ price: 0.49, size: 1000 }], asks: [{ price: 0.51, size: 1000 }] } }
};
const userOrders = { mBids: [{ spreadCents: 1, size: 300 }], mAsks: [{ spreadCents: 1, size: 300 }], mPrimeBids: [{ spreadCents: 1, size: 300 }], mPrimeAsks: [{ spreadCents: 1, size: 300 }] };
const result = projectMarketReward(market, userOrders, 1000);
console.log(JSON.stringify({ marketId: market.id, projectedDailyReward: result.projectedDailyReward, projectedApr: result.projectedApr, note: 'Compare this logged projection vs actual next-day payout manually.' }, null, 2));

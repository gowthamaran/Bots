import { computeMakerScores, projectReward } from './scoring.js';

export function aggregateCompetingQMin(snapshot, v, c = 3) {
  const toOrders = (levels, midpoint) => levels.map((l) => ({ spreadCents: Math.abs((l.price - midpoint) * 100), size: l.size }));
  const midpoint = snapshot.midpoint;
  const book = {
    mBids: toOrders(snapshot.yes.bids, midpoint),
    mAsks: toOrders(snapshot.yes.asks, midpoint),
    mPrimeBids: toOrders(snapshot.no.bids, 1 - midpoint),
    mPrimeAsks: toOrders(snapshot.no.asks, 1 - midpoint)
  };
  return computeMakerScores({ v, c, midpoint, book }).qMin;
}

export function projectMarketReward(market, userOrders, userCapital) {
  const competingQMin = aggregateCompetingQMin(market.snapshot, market.maxIncentiveSpreadCents, market.c);
  const userQMin = computeMakerScores({
    v: market.maxIncentiveSpreadCents,
    c: market.c,
    midpoint: market.snapshot.midpoint,
    book: userOrders
  }).qMin;
  return { ...projectReward({ marketPoolPerDay: market.rewardPoolPerDay, userCapital, userQMin, competingQMin }), userQMin, competingQMin };
}

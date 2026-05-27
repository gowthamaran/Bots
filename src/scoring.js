export function orderScore(v, s, b = 1) {
  if (s >= v) return 0;
  return (((v - s) / v) ** 2) * b;
}

export function computeMakerScores({ v, c = 3, midpoint, book }) {
  const sum = (arr) => arr.reduce((a, o) => a + orderScore(v, o.spreadCents, o.multiplier ?? 1) * o.size, 0);
  const qOne = sum(book.mBids) + sum(book.mPrimeAsks);
  const qTwo = sum(book.mAsks) + sum(book.mPrimeBids);
  const inMidBand = midpoint >= 0.1 && midpoint <= 0.9;
  const qMin = inMidBand ? Math.max(Math.min(qOne, qTwo), Math.max(qOne / c, qTwo / c)) : Math.min(qOne, qTwo);
  return { qOne, qTwo, qMin };
}

export function projectReward({ marketPoolPerDay, userCapital, userQMin, competingQMin }) {
  const projectedShare = userQMin <= 0 ? 0 : userQMin / (userQMin + competingQMin);
  const projectedDailyReward = projectedShare * marketPoolPerDay;
  const projectedApr = userCapital > 0 ? (projectedDailyReward / userCapital) * 365 : 0;
  return { projectedShare, projectedDailyReward, projectedApr, estimate: true };
}

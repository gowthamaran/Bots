import http from 'node:http';
import { projectMarketReward } from './compute.js';

const sampleMarkets = [
  { id: '1', name: 'NBA Finals G1', category: 'sports', status: 'live', rewardPoolPerDay: 1200, maxIncentiveSpreadCents: 3, c: 3,
    snapshot: { midpoint: 0.5, yes: { bids: [{ price: 0.49, size: 800 }], asks: [{ price: 0.51, size: 900 }] }, no: { bids: [{ price: 0.49, size: 700 }], asks: [{ price: 0.51, size: 700 }] } } }
];

function defaultUserOrders() { return { mBids: [{ spreadCents: 1, size: 250 }], mAsks: [{ spreadCents: 1, size: 250 }], mPrimeBids: [{ spreadCents: 1, size: 250 }], mPrimeAsks: [{ spreadCents: 1, size: 250 }] }; }

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/markets') {
    const capital = Number(url.searchParams.get('capital') ?? '500');
    const rows = sampleMarkets.map((m) => ({ ...m, projection: projectMarketReward(m, defaultUserOrders(), capital) }))
      .sort((a, b) => b.projection.projectedApr - a.projection.projectedApr);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(rows));
    return;
  }
  res.statusCode = 404; res.end('Not found');
}).listen(3000);

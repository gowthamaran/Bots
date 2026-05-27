# FINDINGS (verified against Polymarket docs on 2026-05-27)

## Confirmed docs/pages
- Markets: https://docs.polymarket.com/market-data/fetching-markets
- Orderbook: https://docs.polymarket.com/trading/orderbook
- API Intro/limits: https://docs.polymarket.com/api-reference/introduction and https://docs.polymarket.com/quickstart/introduction/rate-limits
- Builder program: https://docs.polymarket.com/builders/overview and tiers page.
- Liquidity rewards: https://docs.polymarket.com/developers/market-makers/liquidity-rewards

## What was confirmed
- `min_incentive_size` and `max_incentive_spread` are explicitly documented as market-object fields fetchable alongside full market objects.
- Orderbook is fetched per `token_id` via CLOB orderbook endpoint; responses include `bids` and `asks` arrays.
- YES/NO token ids are provided in market payloads (docs reference `clobTokenIds` / token IDs for outcomes).
- Liquidity rewards sampling cadence is once per minute and epoch is 10,080 samples (7 days).
- WebSocket orderbook stream exists and should be preferred for live updates; REST fallback for resiliency.
- Rate limits are documented and tier-dependent; when constrained, prioritize higher reward-pool markets and/or request Builder tier upgrades.

## Deviations / caveats surfaced
- Public docs describe fields and capability but exact reward-allocation field naming can vary by endpoint version; implementation keeps this mapped in one adapter so field-name drift is explicit.
- Public orderbook data does not include per-maker attribution, so competing liquidity is estimated in aggregate from visible depth.

## Validation harness status
- Implemented `scripts/validation-harness.js` to log projected daily reward from a snapshot and user order set.
- Real payout comparison must be executed over an actual next-day epoch boundary before publishing numbers to X; this repo currently includes the harness and workflow but not a historical verified payout artifact.

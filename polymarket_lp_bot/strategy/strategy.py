"""Configurable liquidity-rewards strategy.

The v1 allocator is intentionally simple: compute a balanced YES bid/ask quote around the current
midpoint, simulate the incremental Q_min and daily reward share, then only return orders when the
estimated daily yield clears the configured threshold and all risk filters pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from polymarket_lp_bot.config import StrategyConfig
from polymarket_lp_bot.data.data_fetcher import MarketSnapshot
from polymarket_lp_bot.risk import RiskManager
from polymarket_lp_bot.scoring import Outcome, RewardOrder, Side, estimate_daily_reward, score_orders


@dataclass(frozen=True, slots=True)
class OrderIntent:
    condition_id: str
    token_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float
    estimated_daily_reward: float
    estimated_daily_yield_pct: float
    reason: str


class LiquidityRewardsStrategy:
    def __init__(self, config: StrategyConfig, risk: RiskManager):
        self.config = config
        self.risk = risk

    def build_intents(self, snapshot: MarketSnapshot, competitor_orders: list[RewardOrder] | None = None) -> list[OrderIntent]:
        market = snapshot.market
        midpoint = snapshot.midpoint
        if midpoint is None:
            logger.info("{} skipped: no midpoint", market.condition_id)
            return []

        boundary_buffer = self.config.boundary_buffer_cents / 100.0
        if not self.risk.boundary_allowed(snapshot, boundary_buffer):
            logger.info("{} skipped: midpoint {} too close/outside 10c/90c reward boundary", market.condition_id, midpoint)
            return []

        half_spread = self.config.target_spread_cents / 100.0
        bid_price = round(max(0.01, midpoint - half_spread), 2)
        ask_price = round(min(0.99, midpoint + half_spread), 2)
        if abs(bid_price - midpoint) > market.max_incentive_spread or abs(ask_price - midpoint) > market.max_incentive_spread:
            logger.info("{} skipped: target spread wider than max incentive spread", market.condition_id)
            return []

        # Size is share quantity. For a USDC budget, shares ~= notional / price on bids and / (1-price)
        # for asks exposure; v1 uses conservative max(price, 1-price) denominator.
        if market.min_incentive_size <= 0:
            logger.info("{} skipped: missing min incentive size", market.condition_id)
            return []
        max_budget = min(self.config.max_order_size_usdc, self.risk.config.max_market_capital_usdc / 2)
        size = max(market.min_incentive_size, max_budget / max(bid_price, ask_price, 0.01))
        notional = size * max(bid_price, 1.0 - ask_price)
        if not self.risk.capital_allowed(market.condition_id, notional * 2):
            return []

        hypothetical = [
            RewardOrder(Outcome.YES, Side.BID, bid_price, size, owner="me"),
            RewardOrder(Outcome.YES, Side.ASK, ask_price, size, owner="me"),
        ]
        own_score = score_orders(
            hypothetical,
            midpoint=midpoint,
            max_spread=market.max_incentive_spread,
            min_incentive_size=market.min_incentive_size,
        )
        competitor_score = score_orders(
            competitor_orders or [],
            midpoint=midpoint,
            max_spread=market.max_incentive_spread,
            min_incentive_size=market.min_incentive_size,
        )
        estimated_reward = estimate_daily_reward(own_score.q_min, competitor_score.q_min, market.rewards_daily_rate)
        deployed_capital = size * bid_price + size * (1.0 - ask_price)
        daily_yield_pct = 100.0 * estimated_reward / deployed_capital if deployed_capital else 0.0
        if daily_yield_pct < self.config.min_estimated_daily_yield_pct:
            logger.info(
                "{} skipped: estimated daily yield {:.3f}% < threshold {:.3f}%",
                market.condition_id,
                daily_yield_pct,
                self.config.min_estimated_daily_yield_pct,
            )
            return []

        reason = f"q_min={own_score.q_min:.4f} comp_q={competitor_score.q_min:.4f}"
        return [
            OrderIntent(market.condition_id, market.yes_token_id, Outcome.YES, Side.BID, bid_price, size, estimated_reward, daily_yield_pct, reason),
            OrderIntent(market.condition_id, market.yes_token_id, Outcome.YES, Side.ASK, ask_price, size, estimated_reward, daily_yield_pct, reason),
        ]

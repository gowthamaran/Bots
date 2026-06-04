"""Configurable liquidity-rewards and Telegram-driven scalping strategy.

The v1 allocator is intentionally simple: compute balanced quotes around the current midpoint,
simulate incremental Q_min/reward share, require low competition, and only return orders when the
estimated daily yield clears the configured threshold. Filled inventory is not meant to be held; the
execution supervisor immediately tries to sell/flatten positions.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from polymarket_lp_bot.config import MarketFilters, StrategyConfig, TradingPolicyConfig
from polymarket_lp_bot.data.data_fetcher import MarketSnapshot
from polymarket_lp_bot.learning import LearningStore
from polymarket_lp_bot.risk import RiskManager
from polymarket_lp_bot.scoring import (
    Outcome,
    RewardOrder,
    Side,
    contributes_to_q_one,
    contributes_to_q_two,
    estimate_daily_reward,
    score_orders,
    spread_from_midpoint,
)


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


@dataclass(frozen=True, slots=True)
class CompetitionSnapshot:
    q_min: float
    qualifying_q_one_levels: int
    qualifying_q_two_levels: int

    @property
    def is_low_competition(self) -> bool:
        return self.q_min > 0


class LiquidityRewardsStrategy:
    def __init__(
        self,
        config: StrategyConfig,
        risk: RiskManager,
        filters: MarketFilters | None = None,
        policy: TradingPolicyConfig | None = None,
        learning: LearningStore | None = None,
    ):
        self.config = config
        self.risk = risk
        self.filters = filters or MarketFilters()
        self.policy = policy or TradingPolicyConfig()
        self.learning = learning

    def build_intents(self, snapshot: MarketSnapshot, competitor_orders: list[RewardOrder] | None = None) -> list[OrderIntent]:
        market = snapshot.market
        midpoint = snapshot.midpoint
        if midpoint is None:
            logger.info("{} skipped: no midpoint", market.condition_id)
            return []
        if not self.risk.trading_window_open():
            logger.info("{} skipped: overnight no-resting-orders window is active", market.condition_id)
            return []
        if not self.risk.resolution_allowed(market, self.filters.min_days_to_resolution):
            logger.info("{} skipped: resolution is inside {} day minimum", market.condition_id, self.filters.min_days_to_resolution)
            return []

        boundary_buffer = self.config.boundary_buffer_cents / 100.0
        if not self.risk.boundary_allowed(snapshot, boundary_buffer):
            logger.info("{} skipped: midpoint {} too close/outside 10c/90c reward boundary", market.condition_id, midpoint)
            return []

        competitors = competitor_orders if competitor_orders is not None else self.competitor_orders_from_books(snapshot)
        competition = self.competition_snapshot(competitors, snapshot)
        if competition.q_min > self.policy.low_competition_max_q_min:
            logger.info("{} skipped: competition q_min {:.2f} above low-competition cap", market.condition_id, competition.q_min)
            return []
        if (
            competition.qualifying_q_one_levels > self.policy.low_competition_max_qualifying_levels_per_side
            or competition.qualifying_q_two_levels > self.policy.low_competition_max_qualifying_levels_per_side
        ):
            logger.info("{} skipped: too many qualifying competitor levels", market.condition_id)
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
            competitors,
            midpoint=midpoint,
            max_spread=market.max_incentive_spread,
            min_incentive_size=market.min_incentive_size,
        )
        estimated_reward = estimate_daily_reward(own_score.q_min, competitor_score.q_min, market.rewards_daily_rate)
        deployed_capital = size * bid_price + size * (1.0 - ask_price)
        daily_yield_pct = 100.0 * estimated_reward / deployed_capital if deployed_capital else 0.0
        adaptive_hurdle = self.config.min_estimated_daily_yield_pct
        if self.learning:
            adaptive_hurdle += self.learning.market_penalty_bps(market.condition_id)
        self._learn_cycle(market.condition_id, competitor_score.q_min, daily_yield_pct, midpoint)
        if daily_yield_pct < adaptive_hurdle:
            logger.info(
                "{} skipped: estimated daily yield {:.3f}% < adaptive threshold {:.3f}%",
                market.condition_id,
                daily_yield_pct,
                adaptive_hurdle,
            )
            return []

        reason = f"q_min={own_score.q_min:.4f} comp_q={competitor_score.q_min:.4f} low_comp_levels={competition.qualifying_q_one_levels}/{competition.qualifying_q_two_levels}"
        return [
            OrderIntent(market.condition_id, market.yes_token_id, Outcome.YES, Side.BID, bid_price, size, estimated_reward, daily_yield_pct, reason),
            OrderIntent(market.condition_id, market.yes_token_id, Outcome.YES, Side.ASK, ask_price, size, estimated_reward, daily_yield_pct, reason),
        ]

    def competitor_orders_from_books(self, snapshot: MarketSnapshot) -> list[RewardOrder]:
        """Convert visible book levels into competitor RewardOrders for low-competition checks."""

        orders: list[RewardOrder] = []
        for level in snapshot.yes_book.bids:
            orders.append(RewardOrder(Outcome.YES, Side.BID, level.price, level.size, owner="book"))
        for level in snapshot.yes_book.asks:
            orders.append(RewardOrder(Outcome.YES, Side.ASK, level.price, level.size, owner="book"))
        for level in snapshot.no_book.bids:
            orders.append(RewardOrder(Outcome.NO, Side.BID, level.price, level.size, owner="book"))
        for level in snapshot.no_book.asks:
            orders.append(RewardOrder(Outcome.NO, Side.ASK, level.price, level.size, owner="book"))
        return orders

    def competition_snapshot(self, competitor_orders: list[RewardOrder], snapshot: MarketSnapshot) -> CompetitionSnapshot:
        midpoint = snapshot.midpoint or 0.5
        score = score_orders(
            competitor_orders,
            midpoint=midpoint,
            max_spread=snapshot.market.max_incentive_spread,
            min_incentive_size=snapshot.market.min_incentive_size,
        )
        q_one_levels = 0
        q_two_levels = 0
        for order in competitor_orders:
            if order.size < snapshot.market.min_incentive_size:
                continue
            if spread_from_midpoint(order, midpoint) > snapshot.market.max_incentive_spread:
                continue
            if contributes_to_q_one(order):
                q_one_levels += 1
            elif contributes_to_q_two(order):
                q_two_levels += 1
        return CompetitionSnapshot(score.q_min, q_one_levels, q_two_levels)

    def _learn_cycle(self, market_id: str, competition_q_min: float, estimated_yield_pct: float, midpoint: float) -> None:
        if self.learning:
            self.learning.record(
                "cycle_evaluated",
                market_id,
                competition_q_min=competition_q_min,
                estimated_yield_pct=estimated_yield_pct,
                midpoint=midpoint,
            )

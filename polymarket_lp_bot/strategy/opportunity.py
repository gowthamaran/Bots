"""Market ranking, small-bankroll allocation, and spread optimization.

This module turns the article's practical rules into deployable gates. It answers the question that
matters for a small account: "Can my bankroll satisfy min-size on both sides and still clear the $1+
payout threshold after competition?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from polymarket_lp_bot.config import CapitalConfig, MarketFilters, OptimizationConfig, RewardsConfig, TradingPolicyConfig
from polymarket_lp_bot.learning import LearningStore
from polymarket_lp_bot.scoring import Outcome, RewardOrder, Side, estimate_daily_reward, score_orders

if TYPE_CHECKING:
    from polymarket_lp_bot.data.data_fetcher import MarketSnapshot


@dataclass(frozen=True, slots=True)
class TopicRisk:
    score: float
    label: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FarmingZoneStats:
    competitor_q_min: float
    q_one_liquidity_usdc: float
    q_two_liquidity_usdc: float
    total_zone_liquidity_usdc: float
    qualifying_q_one_levels: int
    qualifying_q_two_levels: int


@dataclass(frozen=True, slots=True)
class Opportunity:
    condition_id: str
    question: str
    midpoint: float
    recommended_spread_cents: float
    required_capital_usdc: float
    deployable_capital_usdc: float
    estimated_daily_reward: float
    estimated_daily_yield_pct: float
    projected_share: float
    opportunity_score: float
    topic_risk: TopicRisk
    farming_zone: FarmingZoneStats
    reasons: tuple[str, ...] = field(default_factory=tuple)
    tradable: bool = True


class TopicRiskClassifier:
    """Simple transparent text classifier for event-risk avoidance."""

    SPORTS = {"sports", "nba", "nfl", "mlb", "nhl", "soccer", "tennis", "ufc", "mma"}

    def __init__(self, filters: MarketFilters):
        self.filters = filters

    def classify(self, question: str, category: str | None = None) -> TopicRisk:
        q = question.lower()
        reasons: list[str] = []
        if category and category.lower() in self.SPORTS and not self.filters.allow_sports:
            reasons.append("sports category")
        for keyword in self.filters.avoid_keywords:
            if keyword.lower() in q:
                reasons.append(f"keyword:{keyword}")
        # Negative-risk/correlated groups are not reliably marked by Gamma, so detect common phrases.
        if any(term in q for term in ("winner of", "nominee", "which candidate", "election winner")):
            reasons.append("possible negative-risk/correlated market")
        if reasons:
            return TopicRisk(score=0.0, label="avoid", reasons=tuple(reasons))
        return TopicRisk(score=1.0, label="ok", reasons=())


class OpportunityAnalyzer:
    def __init__(
        self,
        capital: CapitalConfig,
        rewards: RewardsConfig,
        filters: MarketFilters,
        policy: TradingPolicyConfig,
        optimization: OptimizationConfig,
        learning: LearningStore | None = None,
    ):
        self.capital = capital
        self.rewards = rewards
        self.filters = filters
        self.policy = policy
        self.optimization = optimization
        self.learning = learning
        self.topic_classifier = TopicRiskClassifier(filters)

    def analyze(self, snapshot: "MarketSnapshot", competitor_orders: list[RewardOrder]) -> Opportunity:
        market = snapshot.market
        midpoint = snapshot.midpoint or 0.5
        topic = self.topic_classifier.classify(market.question, market.category)
        farming = self.farming_zone_stats(snapshot, competitor_orders)
        deployable = self.capital.deployable_capital_usdc
        reasons: list[str] = []

        if midpoint < self.filters.hard_stop_min_midpoint or midpoint > self.filters.hard_stop_max_midpoint:
            reasons.append("outside hard 15c/85c stop")
        if midpoint < self.filters.preferred_min_midpoint or midpoint > self.filters.preferred_max_midpoint:
            reasons.append("outside preferred 25c/75c band")
        if topic.label == "avoid":
            reasons.extend(topic.reasons)
        if farming.competitor_q_min > self.policy.low_competition_max_q_min:
            reasons.append("competition q_min too high")
        if farming.qualifying_q_one_levels > self.policy.low_competition_max_qualifying_levels_per_side:
            reasons.append("too many Q_one competitor levels")
        if farming.qualifying_q_two_levels > self.policy.low_competition_max_qualifying_levels_per_side:
            reasons.append("too many Q_two competitor levels")

        best = self._best_spread(snapshot, competitor_orders, deployable)
        spread, required_capital, est_reward, est_yield, projected_share = best
        if required_capital > deployable:
            reasons.append(f"min-size requires ${required_capital:.2f}, deployable ${deployable:.2f}")
        if est_reward < self.rewards.min_projected_payout_usdc:
            reasons.append(f"projected reward ${est_reward:.2f} below ${self.rewards.min_projected_payout_usdc:.2f} gate")
        if est_yield < self.rewards.min_expected_daily_yield_pct:
            reasons.append(f"yield {est_yield:.2f}% below {self.rewards.min_expected_daily_yield_pct:.2f}% gate")

        learning_penalty = self.learning.market_penalty_bps(market.condition_id) if self.learning else 0.0
        competition_discount = 1.0 / (1.0 + farming.total_zone_liquidity_usdc / 1_500.0)
        preferred_band_bonus = 1.0 if self.filters.preferred_min_midpoint <= midpoint <= self.filters.preferred_max_midpoint else 0.35
        score = max(0.0, est_reward) * max(0.0, est_yield) * topic.score * competition_discount * preferred_band_bonus
        score = max(0.0, score - learning_penalty)
        return Opportunity(
            condition_id=market.condition_id,
            question=market.question,
            midpoint=midpoint,
            recommended_spread_cents=spread,
            required_capital_usdc=required_capital,
            deployable_capital_usdc=deployable,
            estimated_daily_reward=est_reward,
            estimated_daily_yield_pct=est_yield,
            projected_share=projected_share,
            opportunity_score=score,
            topic_risk=topic,
            farming_zone=farming,
            reasons=tuple(reasons),
            tradable=not reasons,
        )

    def rank(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        return sorted(opportunities, key=lambda opp: (opp.tradable, opp.opportunity_score), reverse=True)

    def farming_zone_stats(self, snapshot: "MarketSnapshot", competitor_orders: list[RewardOrder]) -> FarmingZoneStats:
        midpoint = snapshot.midpoint or 0.5
        market = snapshot.market
        score = score_orders(competitor_orders, midpoint, market.max_incentive_spread, market.min_incentive_size)
        q_one_liq = q_two_liq = 0.0
        q_one_levels = q_two_levels = 0
        for order in competitor_orders:
            if order.size < market.min_incentive_size:
                continue
            yes_price = order.price if order.outcome == Outcome.YES else 1.0 - order.price
            if abs(yes_price - midpoint) > market.max_incentive_spread:
                continue
            notional = order.size * max(0.01, min(0.99, yes_price))
            if (order.outcome == Outcome.YES and order.side == Side.BID) or (order.outcome == Outcome.NO and order.side == Side.ASK):
                q_one_liq += notional
                q_one_levels += 1
            else:
                q_two_liq += notional
                q_two_levels += 1
        return FarmingZoneStats(score.q_min, q_one_liq, q_two_liq, q_one_liq + q_two_liq, q_one_levels, q_two_levels)

    def simulate_24h_reward(self, opportunity: Opportunity, active_minutes: int | None = None) -> float:
        minutes = active_minutes if active_minutes is not None else self.rewards.sample_minutes_per_day
        return opportunity.estimated_daily_reward * max(0, minutes) / max(1, self.rewards.sample_minutes_per_day)

    def _best_spread(self, snapshot: "MarketSnapshot", competitor_orders: list[RewardOrder], deployable: float) -> tuple[float, float, float, float, float]:
        candidates = self.optimization.candidate_spreads_cents if self.optimization.enabled else [1.0]
        best = (candidates[0], float("inf"), 0.0, 0.0, 0.0)
        best_score = -1.0
        for spread_cents in candidates:
            required, est_reward, est_yield, share = self._simulate_spread(snapshot, competitor_orders, spread_cents, deployable)
            # Prefer feasible spreads first, then maximize reward/yield product.
            feasible_bonus = 1_000_000.0 if required <= deployable else 0.0
            candidate_score = feasible_bonus + est_reward * max(est_yield, 0.0)
            if candidate_score > best_score:
                best_score = candidate_score
                best = (spread_cents, required, est_reward, est_yield, share)
        return best

    def _simulate_spread(self, snapshot: "MarketSnapshot", competitor_orders: list[RewardOrder], spread_cents: float, deployable: float) -> tuple[float, float, float, float]:
        market = snapshot.market
        midpoint = snapshot.midpoint or 0.5
        spread = spread_cents / 100.0
        bid = round(max(0.01, midpoint - spread), 2)
        ask = round(min(0.99, midpoint + spread), 2)
        min_size = max(0.0, market.min_incentive_size)
        required_capital = min_size * bid + min_size * max(0.01, 1.0 - ask)
        if required_capital <= 0:
            return required_capital, 0.0, 0.0, 0.0
        size = min_size
        # If extra deployable capital exists, add it symmetrically but keep one-best-market mode concentrated.
        if deployable > required_capital:
            unit_cost = bid + max(0.01, 1.0 - ask)
            size = max(min_size, deployable / unit_cost)
            required_capital = size * unit_cost
        own_orders = [
            RewardOrder(Outcome.YES, Side.BID, bid, size, "me"),
            RewardOrder(Outcome.YES, Side.ASK, ask, size, "me"),
        ]
        own = score_orders(own_orders, midpoint, market.max_incentive_spread, market.min_incentive_size)
        comp = score_orders(competitor_orders, midpoint, market.max_incentive_spread, market.min_incentive_size)
        est_reward = estimate_daily_reward(own.q_min, comp.q_min, market.rewards_daily_rate)
        est_yield = 100.0 * est_reward / required_capital if required_capital else 0.0
        share = own.q_min / (own.q_min + comp.q_min) if own.q_min + comp.q_min > 0 else 0.0
        return required_capital, est_reward, est_yield, share

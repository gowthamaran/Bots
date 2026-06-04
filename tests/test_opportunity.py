from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from polymarket_lp_bot.config import CapitalConfig, MarketFilters, OptimizationConfig, RewardsConfig, TradingPolicyConfig
from polymarket_lp_bot.learning import LearningStore
from polymarket_lp_bot.scoring import Outcome, RewardOrder, Side
from polymarket_lp_bot.strategy import OpportunityAnalyzer


def _snapshot(min_size=25, rewards=50, midpoint=0.5):
    market = SimpleNamespace(
        condition_id="m1",
        question="Will the Fed cut rates in July?",
        category="macro",
        min_incentive_size=min_size,
        max_incentive_spread=0.03,
        rewards_daily_rate=rewards,
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
    )
    return SimpleNamespace(market=market, midpoint=midpoint)


def test_small_bankroll_rejects_unaffordable_min_size():
    analyzer = OpportunityAnalyzer(
        CapitalConfig(starting_bankroll_usdc=20, reserve_usdc=3, max_active_capital_pct=70),
        RewardsConfig(min_projected_payout_usdc=0, min_expected_daily_yield_pct=0),
        MarketFilters(),
        TradingPolicyConfig(low_competition_max_q_min=10_000),
        OptimizationConfig(candidate_spreads_cents=[1.0]),
        LearningStore("/tmp/unused.jsonl", enabled=False),
    )
    opp = analyzer.analyze(_snapshot(min_size=25), [])
    assert opp.required_capital_usdc > opp.deployable_capital_usdc
    assert opp.tradable is False
    assert any("min-size requires" in reason for reason in opp.reasons)


def test_opportunity_ranks_affordable_low_competition_market_tradable():
    analyzer = OpportunityAnalyzer(
        CapitalConfig(starting_bankroll_usdc=20, reserve_usdc=3, max_active_capital_pct=70),
        RewardsConfig(min_projected_payout_usdc=0.01, min_expected_daily_yield_pct=0),
        MarketFilters(preferred_min_midpoint=0.25, preferred_max_midpoint=0.75),
        TradingPolicyConfig(low_competition_max_q_min=10_000),
        OptimizationConfig(candidate_spreads_cents=[0.5, 1.0]),
        LearningStore("/tmp/unused.jsonl", enabled=False),
    )
    competitor = [RewardOrder(Outcome.YES, Side.BID, 0.49, 5, "book")]
    opp = analyzer.analyze(_snapshot(min_size=5), competitor)
    assert opp.tradable is True
    assert opp.estimated_daily_reward > 0
    assert opp.recommended_spread_cents in {0.5, 1.0}

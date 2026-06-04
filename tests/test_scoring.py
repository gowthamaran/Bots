import pytest

from polymarket_lp_bot.scoring.scoring import (
    Outcome,
    RewardOrder,
    Side,
    estimate_daily_reward,
    order_score,
    q_min,
    score_orders,
)


def test_order_score_formula_exact_quadratic():
    assert order_score(0.03, 0.00, multiplier=1.0) == pytest.approx(1.0)
    assert order_score(0.03, 0.015, multiplier=2.0) == pytest.approx(0.5)
    assert order_score(0.03, 0.03, multiplier=1.0) == pytest.approx(0.0)
    assert order_score(0.03, 0.04, multiplier=1.0) == pytest.approx(0.0)


def test_q_min_single_sided_credit_inside_10_90_inclusive():
    assert q_min(90.0, 0.0, midpoint=0.10, c=3.0) == pytest.approx(30.0)
    assert q_min(90.0, 0.0, midpoint=0.50, c=3.0) == pytest.approx(30.0)
    assert q_min(0.0, 90.0, midpoint=0.90, c=3.0) == pytest.approx(30.0)


def test_q_min_strict_min_outside_10_90():
    assert q_min(90.0, 0.0, midpoint=0.0999, c=3.0) == pytest.approx(0.0)
    assert q_min(0.0, 90.0, midpoint=0.9001, c=3.0) == pytest.approx(0.0)
    assert q_min(20.0, 70.0, midpoint=0.95, c=3.0) == pytest.approx(20.0)


def test_score_orders_maps_yes_and_no_books_to_q_one_q_two():
    orders = [
        RewardOrder(Outcome.YES, Side.BID, price=0.49, size=100.0, owner="me"),
        RewardOrder(Outcome.NO, Side.ASK, price=0.51, size=50.0, owner="me"),  # YES bid 0.49
        RewardOrder(Outcome.YES, Side.ASK, price=0.51, size=80.0, owner="me"),
        RewardOrder(Outcome.NO, Side.BID, price=0.49, size=40.0, owner="me"),  # YES ask 0.51
    ]
    result = score_orders(orders, midpoint=0.50, max_spread=0.03, min_incentive_size=10.0)
    per_share_score = ((0.03 - 0.01) / 0.03) ** 2
    assert result.q_one == pytest.approx((100.0 + 50.0) * per_share_score)
    assert result.q_two == pytest.approx((80.0 + 40.0) * per_share_score)
    assert result.q_min == pytest.approx(min(result.q_one, result.q_two))


def test_score_orders_ignores_below_min_size_and_wide_orders():
    orders = [
        RewardOrder(Outcome.YES, Side.BID, price=0.50, size=9.99, owner="me"),
        RewardOrder(Outcome.YES, Side.BID, price=0.45, size=100.0, owner="me"),
        RewardOrder(Outcome.YES, Side.ASK, price=0.50, size=100.0, owner="me"),
    ]
    result = score_orders(orders, midpoint=0.50, max_spread=0.03, min_incentive_size=10.0)
    assert result.q_one == pytest.approx(0.0)
    assert result.q_two == pytest.approx(100.0)
    assert result.q_min == pytest.approx(100.0 / 3.0)


def test_estimate_daily_reward_uses_post_trade_share():
    assert estimate_daily_reward(own_q_min_after=25, competitor_q_min=75, daily_reward_pool=100) == pytest.approx(25)
    assert estimate_daily_reward(own_q_min_after=0, competitor_q_min=0, daily_reward_pool=100) == pytest.approx(0)

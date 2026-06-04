"""Exact Polymarket Liquidity Rewards scoring engine.

The formulas implemented here mirror Polymarket's public Liquidity Rewards docs:

* Order score: S(v, s) = ((v - s) / v)^2 * b
* Q_one and Q_two aggregate qualifying YES/NO bid/ask depth.
* Q_min uses c=3 single-sided credit only while the adjusted midpoint is in [0.10, 0.90];
  outside that band it falls back to strict two-sided min(Q_one, Q_two).

Prices are represented in dollars (0.50 == 50c). The max spread `v` and order spreads are
also represented in dollars, so a 3c rewards spread is `0.03`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

CENT = 0.01
DEFAULT_C = 3.0
LOWER_SINGLE_SIDED_BOUNDARY = 0.10
UPPER_SINGLE_SIDED_BOUNDARY = 0.90


class Outcome(StrEnum):
    """Binary outcome token leg."""

    YES = "YES"
    NO = "NO"


class Side(StrEnum):
    """Order-book side from the token perspective."""

    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True, slots=True)
class RewardOrder:
    """A resting order candidate used by the scoring engine.

    Attributes:
        outcome: YES or NO token book. In Polymarket binary markets, buying NO is the economic
            complement of selling YES, which is why Q_one/Q_two combine opposite sides.
        side: BID or ASK on that token book.
        price: Limit price in dollars, e.g. 0.49.
        size: Share-denominated quantity. Callers must enforce `min_incentive_size`; scoring can
            ignore smaller orders when the market config is supplied.
        owner: Optional maker identifier used to group competitor and hypothetical orders.
    """

    outcome: Outcome
    side: Side
    price: float
    size: float
    owner: str = "unknown"


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Detailed scoring result for a set of orders owned by one maker."""

    q_one: float
    q_two: float
    q_min: float
    midpoint: float
    max_spread: float
    single_sided_credit_allowed: bool


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be a finite probability in [0, 1], got {value!r}")


def order_score(max_spread: float, spread: float, multiplier: float = 1.0) -> float:
    """Return S(v, s) = ((v - s) / v)^2 * b for a qualifying order.

    Orders at or beyond the max spread receive zero. A negative spread (crossing better than the
    adjusted midpoint) is clamped to zero distance to avoid over-rewarding crossed books in local
    simulation; the live exchange should reject invalid/self-crossing orders before this point.
    """

    if max_spread <= 0 or not isfinite(max_spread):
        raise ValueError("max_spread must be positive and finite")
    if multiplier < 0 or not isfinite(multiplier):
        raise ValueError("multiplier must be non-negative and finite")
    if not isfinite(spread):
        raise ValueError("spread must be finite")

    bounded_spread = min(max(spread, 0.0), max_spread)
    return ((max_spread - bounded_spread) / max_spread) ** 2 * multiplier


def yes_equivalent_price(order: RewardOrder) -> float:
    """Convert any YES/NO token order into an economic YES-price level.

    * YES bid/ask prices are already YES prices.
    * A NO ask at p is equivalent to a YES bid at 1-p.
    * A NO bid at p is equivalent to a YES ask at 1-p.
    """

    _validate_probability(order.price, "order.price")
    if order.outcome == Outcome.YES:
        return order.price
    return 1.0 - order.price


def spread_from_midpoint(order: RewardOrder, midpoint: float) -> float:
    """Return the absolute distance from adjusted midpoint on the YES-price axis."""

    _validate_probability(midpoint, "midpoint")
    return abs(yes_equivalent_price(order) - midpoint)


def contributes_to_q_one(order: RewardOrder) -> bool:
    """Q_one combines YES bids and NO asks, i.e. liquidity supporting the YES-bid side."""

    return (order.outcome == Outcome.YES and order.side == Side.BID) or (
        order.outcome == Outcome.NO and order.side == Side.ASK
    )


def contributes_to_q_two(order: RewardOrder) -> bool:
    """Q_two combines YES asks and NO bids, i.e. liquidity supporting the YES-ask side."""

    return (order.outcome == Outcome.YES and order.side == Side.ASK) or (
        order.outcome == Outcome.NO and order.side == Side.BID
    )


def q_min(q_one: float, q_two: float, midpoint: float, c: float = DEFAULT_C) -> float:
    """Compute Polymarket Q_min with the documented c=3 boundary behavior.

    If midpoint is in [0.10, 0.90], single-sided liquidity receives reduced credit:
    max(min(q_one, q_two), max(q_one/c, q_two/c)). Outside that range, strict two-sided liquidity
    is required and the score is min(q_one, q_two).
    """

    _validate_probability(midpoint, "midpoint")
    if c <= 0 or not isfinite(c):
        raise ValueError("c must be positive and finite")
    if q_one < 0 or q_two < 0:
        raise ValueError("q_one and q_two must be non-negative")

    if LOWER_SINGLE_SIDED_BOUNDARY <= midpoint <= UPPER_SINGLE_SIDED_BOUNDARY:
        return max(min(q_one, q_two), max(q_one / c, q_two / c))
    return min(q_one, q_two)


def score_orders(
    orders: list[RewardOrder],
    midpoint: float,
    max_spread: float,
    min_incentive_size: float = 0.0,
    multiplier: float = 1.0,
    c: float = DEFAULT_C,
) -> ScoreBreakdown:
    """Score one maker's orders for a single sample.

    Callers should pass only the orders for a specific owner when computing per-maker Q. Orders below
    `min_incentive_size`, zero-size orders, or orders wider than `max_spread` contribute zero.
    """

    _validate_probability(midpoint, "midpoint")
    if min_incentive_size < 0:
        raise ValueError("min_incentive_size cannot be negative")

    q_one_total = 0.0
    q_two_total = 0.0
    for order in orders:
        if order.size < min_incentive_size or order.size <= 0:
            continue
        spread = spread_from_midpoint(order, midpoint)
        if spread > max_spread:
            continue
        contribution = order_score(max_spread, spread, multiplier) * order.size
        if contributes_to_q_one(order):
            q_one_total += contribution
        elif contributes_to_q_two(order):
            q_two_total += contribution

    min_score = q_min(q_one_total, q_two_total, midpoint, c=c)
    return ScoreBreakdown(
        q_one=q_one_total,
        q_two=q_two_total,
        q_min=min_score,
        midpoint=midpoint,
        max_spread=max_spread,
        single_sided_credit_allowed=LOWER_SINGLE_SIDED_BOUNDARY <= midpoint <= UPPER_SINGLE_SIDED_BOUNDARY,
    )


def normalized_share(own_q_min: float, total_q_min: float) -> float:
    """Return Q_normal = own Q_min / sum(all makers' Q_min), guarded for empty books."""

    if own_q_min < 0 or total_q_min < 0:
        raise ValueError("Q values must be non-negative")
    if total_q_min == 0:
        return 0.0
    return own_q_min / total_q_min


def estimate_daily_reward(
    own_q_min_after: float,
    competitor_q_min: float,
    daily_reward_pool: float,
) -> float:
    """Estimate dollars/day earned if the current sample share persisted all day."""

    if daily_reward_pool < 0:
        raise ValueError("daily_reward_pool must be non-negative")
    share = normalized_share(own_q_min_after, own_q_min_after + competitor_q_min)
    return share * daily_reward_pool

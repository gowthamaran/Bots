"""Simple v1 risk model and circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass, field

from polymarket_lp_bot.config import RiskConfig
from polymarket_lp_bot.data.data_fetcher import MarketSnapshot


@dataclass
class InventoryState:
    yes_notional: dict[str, float] = field(default_factory=dict)
    no_notional: dict[str, float] = field(default_factory=dict)

    def delta(self, condition_id: str) -> float:
        return self.yes_notional.get(condition_id, 0.0) - self.no_notional.get(condition_id, 0.0)


@dataclass
class CircuitBreaker:
    max_errors: int
    consecutive_errors: int = 0
    tripped: bool = False

    def record_success(self) -> None:
        self.consecutive_errors = 0

    def record_error(self) -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.max_errors:
            self.tripped = True


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.inventory = InventoryState()

    def capital_allowed(self, condition_id: str, proposed_notional: float) -> bool:
        market_used = abs(self.inventory.delta(condition_id)) + proposed_notional
        return proposed_notional <= self.config.max_market_capital_usdc and market_used <= self.config.max_market_capital_usdc

    def boundary_allowed(self, snapshot: MarketSnapshot, boundary_buffer: float) -> bool:
        midpoint = snapshot.midpoint
        if midpoint is None:
            return False
        if not self.config.pause_on_boundary_cross:
            return True
        lower = 0.10 + boundary_buffer
        upper = 0.90 - boundary_buffer
        return lower <= midpoint <= upper

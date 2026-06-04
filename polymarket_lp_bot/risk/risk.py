"""Risk model, no-overnight policy, and circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Any

from polymarket_lp_bot.config import RiskConfig, TradingPolicyConfig

if TYPE_CHECKING:
    from polymarket_lp_bot.data.data_fetcher import Market, MarketSnapshot
else:
    Market = Any
    MarketSnapshot = Any


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
    def __init__(self, config: RiskConfig, policy: TradingPolicyConfig | None = None):
        self.config = config
        self.policy = policy or TradingPolicyConfig()
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

    def resolution_allowed(self, market: Market, min_days: float) -> bool:
        """Only trade markets resolving farther out than the configured horizon."""

        if market.end_date is None:
            return False
        days = (market.end_date - datetime.now(timezone.utc)).total_seconds() / 86_400
        return days >= min_days

    def trading_window_open(self, now: datetime | None = None) -> bool:
        """Return False during the overnight no-resting-orders window."""

        if not self.policy.never_leave_orders_overnight:
            return True
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cancel_after = self._parse_hhmm(self.policy.overnight_cancel_after_utc)
        resume_after = self._parse_hhmm(self.policy.resume_trading_after_utc)
        current = now.time().replace(second=0, microsecond=0, tzinfo=None)
        if cancel_after <= resume_after:
            return not (cancel_after <= current < resume_after)
        return resume_after <= current < cancel_after

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))

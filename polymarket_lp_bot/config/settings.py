"""YAML + environment configuration for the LP bot.

Secrets are read exclusively from environment variables. The YAML file contains strategy/risk knobs
and non-secret endpoint settings so it can be safely committed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - tests may run without optional deps installed
    yaml = None
try:
    from pydantic import BaseModel, Field, SecretStr, field_validator
except ModuleNotFoundError:  # pragma: no cover - lightweight fallback for dependency-free formula tests
    from dataclasses import dataclass
    from typing import Any

    def Field(default: Any = None, default_factory: Any = None):
        return default_factory() if default_factory is not None else default

    class SecretStr:
        def __init__(self, value: str):
            self._value = value

        def get_secret_value(self) -> str:
            return self._value

    def field_validator(*args: Any, **kwargs: Any):
        def decorator(fn: Any) -> Any:
            return fn
        return decorator

    class BaseModel:
        def __init__(self, **kwargs: Any):
            for cls in reversed(self.__class__.mro()):
                for name in getattr(cls, "__annotations__", {}):
                    if name in kwargs:
                        value = kwargs[name]
                    else:
                        value = getattr(self.__class__, name, None)
                    setattr(self, name, value)

        @classmethod
        def model_validate(cls, raw: dict[str, Any]):
            return cls(**raw)

        def model_copy(self, update: dict[str, Any] | None = None):
            data = {name: getattr(self, name) for name in getattr(self.__class__, "__annotations__", {})}
            if update:
                data.update(update)
            return self.__class__(**data)


class ApiConfig(BaseModel):
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    chain_id: int = 137
    request_timeout_seconds: float = 10.0
    max_retries: int = 4


class MarketFilters(BaseModel):
    min_midpoint: float = 0.10
    max_midpoint: float = 0.90
    preferred_min_midpoint: float = 0.25
    preferred_max_midpoint: float = 0.75
    hard_stop_min_midpoint: float = 0.15
    hard_stop_max_midpoint: float = 0.85
    min_days_to_resolution: float = 7.0
    min_daily_rewards_usdc: float = 10.0
    min_liquidity_usdc: float = 1_000.0
    min_volume_24h_usdc: float = 1_000.0
    min_hours_to_resolution: float = 24.0
    max_hours_to_resolution: float = 24 * 60.0
    allowed_topics: list[str] = Field(default_factory=list)
    blocked_keywords: list[str] = Field(default_factory=lambda: ["lawsuit", "court", "delist"])
    avoid_keywords: list[str] = Field(default_factory=lambda: ["tweet", "mention", "say", "war", "invasion", "attack", "ceasefire", "lawsuit", "court", "bankruptcy", "celebrity"])
    allow_sports: bool = False


class StrategyConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    loop_interval_seconds: int = 60
    target_spread_cents: float = 1.0
    min_estimated_daily_yield_pct: float = 0.10
    max_order_size_usdc: float = 100.0
    markets: list[str] = Field(default_factory=list)
    auto_discover: bool = True
    boundary_buffer_cents: float = 1.0
    cancel_stale_orders: bool = True


class CapitalConfig(BaseModel):
    starting_bankroll_usdc: float = 20.0
    reserve_usdc: float = 3.0
    max_active_capital_pct: float = 70.0
    max_concurrent_markets: int = 1
    small_bankroll_mode: bool = True

    @property
    def deployable_capital_usdc(self) -> float:
        gross = max(0.0, self.starting_bankroll_usdc - self.reserve_usdc)
        return gross * max(0.0, min(self.max_active_capital_pct, 100.0)) / 100.0


class RewardsConfig(BaseModel):
    min_projected_payout_usdc: float = 1.30
    min_expected_daily_yield_pct: float = 6.5
    payout_safety_buffer: float = 1.30
    sample_minutes_per_day: int = 1_440


class OptimizationConfig(BaseModel):
    enabled: bool = True
    candidate_spreads_cents: list[float] = Field(default_factory=lambda: [0.5, 1.0, 1.5])
    quote_refresh_seconds: int = 15
    max_q_imbalance_ratio: float = 1.5
    auto_trade_top_market: bool = True
    backtest_enabled: bool = False


class PnlConfig(BaseModel):
    path: str = "data/pnl_events.jsonl"
    reward_reconciliation_minutes_after_midnight: int = 5


class RiskConfig(BaseModel):
    max_total_capital_usdc: float = 1_000.0
    max_market_capital_usdc: float = 200.0
    max_inventory_delta_usdc: float = 75.0
    max_live_orders_per_market: int = 4
    circuit_breaker_consecutive_errors: int = 5
    pause_on_boundary_cross: bool = True


class TradingPolicyConfig(BaseModel):
    """Hard safety rules requested for the Telegram-controlled scalping workflow."""

    never_leave_orders_overnight: bool = True
    overnight_cancel_after_utc: str = "23:30"
    resume_trading_after_utc: str = "00:10"
    flatten_positions_immediately: bool = True
    position_poll_interval_seconds: int = 15
    order_refresh_seconds: int = 60
    low_competition_max_q_min: float = 2_500.0
    low_competition_max_qualifying_levels_per_side: int = 4
    marketable_exit_edge_cents: float = 1.0


class LearningConfig(BaseModel):
    enabled: bool = True
    path: str = "data/learning_events.jsonl"


class TelegramConfig(BaseModel):
    enabled: bool = False
    chat_id: str | None = None
    token: SecretStr | None = None
    command_poll_seconds: float = 1.0


class Secrets(BaseModel):
    polymarket_private_key: SecretStr | None = None
    polymarket_funder: str | None = None
    polymarket_signature_type: int | None = None


class BotConfig(BaseModel):
    api: ApiConfig = Field(default_factory=ApiConfig)
    filters: MarketFilters = Field(default_factory=MarketFilters)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    policy: TradingPolicyConfig = Field(default_factory=TradingPolicyConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    rewards: RewardsConfig = Field(default_factory=RewardsConfig)
    optimization: OptimizationConfig = Field(default_factory=OptimizationConfig)
    pnl: PnlConfig = Field(default_factory=PnlConfig)
    secrets: Secrets = Field(default_factory=Secrets)

    @field_validator("strategy")
    @classmethod
    def live_requires_env_key(cls, strategy: StrategyConfig):
        # Validation of secrets happens after env merge in load_config.
        return strategy


def load_config(path: str | Path) -> BotConfig:
    if Path(path).exists():
        if yaml is None:
            raise RuntimeError("PyYAML is required to load YAML config; install project dependencies")
        raw = yaml.safe_load(Path(path).read_text())
    else:
        raw = {}
    cfg = BotConfig.model_validate(raw or {})

    cfg.telegram.token = SecretStr(os.environ["TELEGRAM_BOT_TOKEN"]) if os.getenv("TELEGRAM_BOT_TOKEN") else cfg.telegram.token
    cfg.telegram.chat_id = os.getenv("TELEGRAM_CHAT_ID", cfg.telegram.chat_id)
    cfg.secrets.polymarket_private_key = (
        SecretStr(os.environ["POLYMARKET_PRIVATE_KEY"])
        if os.getenv("POLYMARKET_PRIVATE_KEY")
        else cfg.secrets.polymarket_private_key
    )
    cfg.secrets.polymarket_funder = os.getenv("POLYMARKET_FUNDER", cfg.secrets.polymarket_funder)
    if os.getenv("POLYMARKET_SIGNATURE_TYPE"):
        cfg.secrets.polymarket_signature_type = int(os.environ["POLYMARKET_SIGNATURE_TYPE"])

    if cfg.strategy.mode == "live" and cfg.secrets.polymarket_private_key is None:
        raise ValueError("live mode requires POLYMARKET_PRIVATE_KEY in the environment")
    return cfg

"""YAML + environment configuration for the LP bot.

Secrets are read exclusively from environment variables. The YAML file contains strategy/risk knobs
and non-secret endpoint settings so it can be safely committed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator


class ApiConfig(BaseModel):
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    chain_id: int = 137
    request_timeout_seconds: float = 10.0
    max_retries: int = 4


class MarketFilters(BaseModel):
    min_midpoint: float = 0.10
    max_midpoint: float = 0.90
    min_daily_rewards_usdc: float = 10.0
    min_liquidity_usdc: float = 1_000.0
    min_volume_24h_usdc: float = 1_000.0
    min_hours_to_resolution: float = 24.0
    max_hours_to_resolution: float = 24 * 60.0
    allowed_topics: list[str] = Field(default_factory=list)
    blocked_keywords: list[str] = Field(default_factory=lambda: ["lawsuit", "court", "delist"])
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


class RiskConfig(BaseModel):
    max_total_capital_usdc: float = 1_000.0
    max_market_capital_usdc: float = 200.0
    max_inventory_delta_usdc: float = 75.0
    max_live_orders_per_market: int = 4
    circuit_breaker_consecutive_errors: int = 5
    pause_on_boundary_cross: bool = True


class TelegramConfig(BaseModel):
    enabled: bool = False
    chat_id: str | None = None
    token: SecretStr | None = None


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
    secrets: Secrets = Field(default_factory=Secrets)

    @field_validator("strategy")
    @classmethod
    def live_requires_env_key(cls, strategy: StrategyConfig):
        # Validation of secrets happens after env merge in load_config.
        return strategy


def load_config(path: str | Path) -> BotConfig:
    raw = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
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

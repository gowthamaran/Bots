"""Async Polymarket Gamma/CLOB data layer with retries and normalized models."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
try:
    from loguru import logger
except ModuleNotFoundError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from polymarket_lp_bot.config import ApiConfig, MarketFilters


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)


class Market(BaseModel):
    condition_id: str
    question: str
    slug: str | None = None
    yes_token_id: str
    no_token_id: str
    active: bool = True
    closed: bool = False
    end_date: datetime | None = None
    liquidity: float = 0.0
    volume_24h: float = 0.0
    rewards_daily_rate: float = 0.0
    min_incentive_size: float = 0.0
    max_incentive_spread: float = 0.03
    category: str | None = None


class MarketSnapshot(BaseModel):
    market: Market
    yes_book: OrderBook
    no_book: OrderBook

    @property
    def midpoint(self) -> float | None:
        """Size-cutoff adjusted midpoint approximation from current best YES bid/ask.

        Polymarket's production scorer uses its size-cutoff-adjusted midpoint. In v1 we approximate
        it from top-of-book data after filtering the strategy's target orders by min incentive size.
        This method is deliberately isolated so a future data adapter can replace it with the exact
        CLOB midpoint endpoint if/when exposed.
        """

        bid = self.yes_book.best_bid
        ask = self.yes_book.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        no_bid = self.no_book.best_bid
        no_ask = self.no_book.best_ask
        if no_bid is not None and no_ask is not None:
            return 1.0 - ((no_bid + no_ask) / 2)
        return None


class PolymarketDataFetcher:
    def __init__(self, api: ApiConfig):
        self.api = api
        self._client = httpx.AsyncClient(timeout=api.request_timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def discover_reward_markets(self, filters: MarketFilters, limit: int = 100) -> list[Market]:
        """Fetch active reward-enabled markets and apply Herman-style practical filters.

        The Gamma API shape has changed over time; parsing is defensive and accepts common aliases.
        """

        url = f"{self.api.gamma_base_url}/markets"
        raw = await self._get(url, {"active": "true", "closed": "false", "limit": limit})
        items = raw if isinstance(raw, list) else raw.get("markets", raw.get("data", []))
        markets = [m for m in (self._parse_market(item) for item in items) if m is not None]
        return [market for market in markets if self._passes_filters(market, filters)]

    async def fetch_configured_markets(self, condition_ids: list[str]) -> list[Market]:
        if not condition_ids:
            return []
        results = await asyncio.gather(*(self.fetch_market(cid) for cid in condition_ids), return_exceptions=True)
        markets: list[Market] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("market fetch failed: {}", result)
            elif result is not None:
                markets.append(result)
        return markets

    async def fetch_market(self, condition_id: str) -> Market | None:
        raw = await self._get(f"{self.api.gamma_base_url}/markets/{condition_id}")
        return self._parse_market(raw)

    async def fetch_snapshot(self, market: Market) -> MarketSnapshot:
        yes_book, no_book = await asyncio.gather(
            self.fetch_order_book(market.yes_token_id), self.fetch_order_book(market.no_token_id)
        )
        return MarketSnapshot(market=market, yes_book=yes_book, no_book=no_book)

    async def fetch_order_book(self, token_id: str) -> OrderBook:
        raw = await self._get(f"{self.api.clob_base_url}/book", {"token_id": token_id})
        return OrderBook(
            token_id=token_id,
            bids=[BookLevel(price=float(x["price"]), size=float(x["size"])) for x in raw.get("bids", [])],
            asks=[BookLevel(price=float(x["price"]), size=float(x["size"])) for x in raw.get("asks", [])],
        )

    def _parse_market(self, item: dict[str, Any]) -> Market | None:
        try:
            tokens = item.get("tokens") or item.get("clobTokenIds") or []
            if isinstance(tokens, str):
                import json

                tokens = json.loads(tokens)
            yes_token = tokens[0]["token_id"] if tokens and isinstance(tokens[0], dict) else tokens[0]
            no_token = tokens[1]["token_id"] if len(tokens) > 1 and isinstance(tokens[1], dict) else tokens[1]
            rewards = item.get("rewards") or {}
            return Market(
                condition_id=item.get("conditionId") or item.get("condition_id") or item.get("id"),
                question=item.get("question", ""),
                slug=item.get("slug"),
                yes_token_id=str(yes_token),
                no_token_id=str(no_token),
                active=bool(item.get("active", True)),
                closed=bool(item.get("closed", False)),
                end_date=self._parse_dt(item.get("endDate") or item.get("end_date_iso")),
                liquidity=float(item.get("liquidity", item.get("liquidityNum", 0)) or 0),
                volume_24h=float(item.get("volume24hr", item.get("volume24h", 0)) or 0),
                rewards_daily_rate=float(
                    rewards.get("daily_rate", item.get("rewardsDailyRate", item.get("reward", 0))) or 0
                ),
                min_incentive_size=float(
                    item.get("min_incentive_size", rewards.get("min_size", rewards.get("min_incentive_size", 0))) or 0
                ),
                max_incentive_spread=float(
                    item.get("max_incentive_spread", rewards.get("max_spread", rewards.get("max_incentive_spread", 3)))
                    or 3
                )
                / 100.0,
                category=item.get("category"),
            )
        except Exception as exc:  # noqa: BLE001 - defensive parser for third-party JSON
            logger.debug("could not parse Gamma market {}: {}", item.get("id"), exc)
            return None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _passes_filters(self, market: Market, filters: MarketFilters) -> bool:
        if not market.active or market.closed:
            return False
        if market.rewards_daily_rate < filters.min_daily_rewards_usdc:
            return False
        if market.liquidity < filters.min_liquidity_usdc or market.volume_24h < filters.min_volume_24h_usdc:
            return False
        if market.end_date:
            hours = (market.end_date - datetime.now(timezone.utc)).total_seconds() / 3600
            days = hours / 24
            if days < filters.min_days_to_resolution:
                return False
            if hours < filters.min_hours_to_resolution or hours > filters.max_hours_to_resolution:
                return False
        else:
            return False
        q = market.question.lower()
        if any(keyword.lower() in q for keyword in filters.blocked_keywords):
            return False
        if any(keyword.lower() in q for keyword in filters.avoid_keywords):
            return False
        if filters.allowed_topics and (market.category or "").lower() not in {x.lower() for x in filters.allowed_topics}:
            return False
        if not filters.allow_sports and (market.category or "").lower() in {"sports", "nba", "nfl", "mlb", "nhl"}:
            return False
        return True

"""Async Polymarket Gamma/CLOB data layer with retries and normalized models.

The important fix here is discovery: reward markets should come from the CLOB rewards endpoint,
not only from Gamma. Gamma fields have changed over time and many reward-specific fields are absent
or camelCased, which caused the bot to find zero markets even while Polymarket had active rewards.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - parser tests before deps install
    class _HTTPError(Exception):
        pass

    class _TimeoutException(Exception):
        pass

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, *args, **kwargs):
            raise RuntimeError("httpx is required for network calls; install project dependencies")

        async def aclose(self):
            pass

    class httpx:  # type: ignore[no-redef]
        HTTPError = _HTTPError
        TimeoutException = _TimeoutException
        AsyncClient = _AsyncClient
try:
    from loguru import logger
except ModuleNotFoundError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)
try:
    from pydantic import BaseModel, Field
except ModuleNotFoundError:  # pragma: no cover - lets parser tests run before deps are installed
    from typing import Any as _Any

    def Field(default: _Any = None, default_factory: _Any = None):
        return default_factory() if default_factory is not None else default

    class BaseModel:
        def __init__(self, **kwargs: _Any):
            for cls in reversed(self.__class__.mro()):
                for name in getattr(cls, "__annotations__", {}):
                    setattr(self, name, kwargs.get(name, getattr(self.__class__, name, None)))
try:
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ModuleNotFoundError:  # pragma: no cover
    def retry(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def retry_if_exception_type(*args, **kwargs):
        return None

    def stop_after_attempt(*args, **kwargs):
        return None

    def wait_exponential(*args, **kwargs):
        return None

from polymarket_lp_bot.config import ApiConfig, MarketFilters

LAST_CURSOR = "LTE="


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
    source: str = "unknown"


class MarketSnapshot(BaseModel):
    market: Market
    yes_book: OrderBook
    no_book: OrderBook

    @property
    def midpoint(self) -> float | None:
        bid = self.yes_book.best_bid
        ask = self.yes_book.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        no_bid = self.no_book.best_bid
        no_ask = self.no_book.best_ask
        if no_bid is not None and no_ask is not None:
            return 1.0 - ((no_bid + no_ask) / 2)
        # Last resort for paper discovery: reward endpoint token prices are often fresher than Gamma.
        return None


class PolymarketDataFetcher:
    def __init__(self, api: ApiConfig):
        self.api = api
        self._client = httpx.AsyncClient(timeout=api.request_timeout_seconds)
        self.last_discovery_stats: dict[str, Any] = {}

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
        """Fetch active reward markets from CLOB rewards API, with Gamma fallback.

        `/rewards/markets/multi` is the public endpoint documented for reward-enabled markets. This
        fixes the previous zero-result behavior from scanning generic Gamma markets and then filtering
        on reward fields that were not present/parsed.
        """

        raw_markets = await self._fetch_clob_reward_markets(limit=limit)
        source = "clob_rewards"
        if not raw_markets:
            logger.warning("CLOB rewards discovery returned no markets; falling back to Gamma markets")
            raw_markets = await self._fetch_gamma_markets(limit=limit)
            source = "gamma"

        parsed = [m for m in (self._parse_market(item, source=source) for item in raw_markets) if m is not None]
        passed: list[Market] = []
        rejected: dict[str, int] = {}
        for market in parsed:
            ok, reason = self._passes_filters(market, filters)
            if ok:
                passed.append(market)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
        self.last_discovery_stats = {
            "source": source,
            "raw": len(raw_markets),
            "parsed": len(parsed),
            "passed": len(passed),
            "rejected": rejected,
        }
        logger.info("discovery stats: {}", self.last_discovery_stats)
        return passed

    async def _fetch_clob_reward_markets(self, limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(items) < limit:
            params: dict[str, Any] = {"page_size": min(500, max(1, limit - len(items))), "order_by": "rate_per_day", "position": "DESC"}
            if cursor:
                params["next_cursor"] = cursor
            raw = await self._get(f"{self.api.clob_base_url}/rewards/markets/multi", params)
            batch = raw.get("data", []) if isinstance(raw, dict) else raw
            items.extend(batch)
            cursor = raw.get("next_cursor") if isinstance(raw, dict) else None
            if not cursor or cursor == LAST_CURSOR or not batch:
                break
        return items[:limit]

    async def _fetch_gamma_markets(self, limit: int) -> list[dict[str, Any]]:
        raw = await self._get(f"{self.api.gamma_base_url}/markets", {"active": "true", "closed": "false", "limit": limit})
        return raw if isinstance(raw, list) else raw.get("markets", raw.get("data", []))

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
        # Prefer reward endpoint for configured markets so min size/max spread/rate are present.
        try:
            raw = await self._get(f"{self.api.clob_base_url}/rewards/markets/{condition_id}")
            items = raw.get("data", []) if isinstance(raw, dict) else raw
            if items:
                return self._parse_market(items[0], source="clob_rewards")
        except Exception as exc:  # noqa: BLE001
            logger.debug("reward market fetch failed for {}: {}", condition_id, exc)
        raw = await self._get(f"{self.api.gamma_base_url}/markets/{condition_id}")
        return self._parse_market(raw, source="gamma")

    async def fetch_snapshot(self, market: Market) -> MarketSnapshot:
        yes_book, no_book = await asyncio.gather(
            self.fetch_order_book(market.yes_token_id), self.fetch_order_book(market.no_token_id)
        )
        return MarketSnapshot(market=market, yes_book=yes_book, no_book=no_book)

    async def fetch_order_book(self, token_id: str) -> OrderBook:
        raw = await self._get(f"{self.api.clob_base_url}/book", {"token_id": token_id})
        return OrderBook(
            token_id=token_id,
            bids=[self._parse_level(x) for x in raw.get("bids", [])],
            asks=[self._parse_level(x) for x in raw.get("asks", [])],
        )

    def _parse_market(self, item: dict[str, Any], source: str = "unknown") -> Market | None:
        try:
            yes_token, no_token = self._extract_tokens(item)
            rewards = item.get("rewards") or {}
            rewards_config = item.get("rewards_config") or rewards.get("rates") or []
            daily_rate = self._first_number(
                item,
                "total_daily_rate",
                "rewardsDailyRate",
                "reward",
                "native_daily_rate",
            )
            if daily_rate == 0 and rewards_config:
                daily_rate = sum(float(cfg.get("rate_per_day", 0) or 0) for cfg in rewards_config)
            if item.get("sponsored_daily_rate") and item.get("native_daily_rate"):
                daily_rate = float(item.get("sponsored_daily_rate") or 0) + float(item.get("native_daily_rate") or 0)

            min_size = self._first_number(item, "rewards_min_size", "rewardsMinSize", default=None)
            if min_size is None:
                min_size = self._first_number(rewards, "min_size", "min_incentive_size", default=0)
            max_spread_raw = self._first_number(item, "rewards_max_spread", "rewardsMaxSpread", default=None)
            if max_spread_raw is None:
                max_spread_raw = self._first_number(rewards, "max_spread", "max_incentive_spread", default=3)

            return Market(
                condition_id=str(item.get("condition_id") or item.get("conditionId") or item.get("id")),
                question=item.get("question") or item.get("group_item_title") or "",
                slug=item.get("market_slug") or item.get("slug"),
                yes_token_id=str(yes_token),
                no_token_id=str(no_token),
                active=bool(item.get("active", True)),
                closed=bool(item.get("closed", False)),
                end_date=self._parse_dt(item.get("end_date") or item.get("endDate") or item.get("endDateIso") or item.get("end_date_iso")),
                liquidity=self._first_number(item, "liquidity", "liquidityNum", default=0),
                volume_24h=self._first_number(item, "volume_24hr", "volume24hr", "volume24h", default=0),
                rewards_daily_rate=float(daily_rate or 0),
                min_incentive_size=float(min_size or 0),
                max_incentive_spread=self._normalize_spread(float(max_spread_raw or 3)),
                category=item.get("category") or item.get("event_slug"),
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - defensive parser for third-party JSON
            logger.debug("could not parse market {}: {}", item.get("id") or item.get("condition_id"), exc)
            return None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _parse_level(level: dict[str, Any] | list[Any]) -> BookLevel:
        if isinstance(level, dict):
            return BookLevel(price=float(level["price"]), size=float(level["size"]))
        return BookLevel(price=float(level[0]), size=float(level[1]))

    @staticmethod
    def _normalize_spread(value: float) -> float:
        # API examples use values like 3 or 99 for cents; some clients may return 0.03.
        return value / 100.0 if value > 1 else value

    @staticmethod
    def _first_number(item: dict[str, Any], *keys: str, default: float | None = 0) -> float | None:
        for key in keys:
            value = item.get(key)
            if value is not None and value != "":
                return float(value)
        return default

    @staticmethod
    def _extract_tokens(item: dict[str, Any]) -> tuple[str, str]:
        tokens = item.get("tokens") or item.get("clobTokenIds") or item.get("clob_token_ids") or []
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if tokens and isinstance(tokens[0], dict):
            yes = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), tokens[0])
            no = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), tokens[1])
            return str(yes["token_id"]), str(no["token_id"])
        return str(tokens[0]), str(tokens[1])

    def _passes_filters(self, market: Market, filters: MarketFilters) -> tuple[bool, str]:
        if not market.active or market.closed:
            return False, "inactive_or_closed"
        if market.rewards_daily_rate <= 0:
            return False, "missing_reward_rate"
        if market.rewards_daily_rate < filters.min_daily_rewards_usdc:
            return False, "low_daily_rewards"
        # Rewards endpoint often omits liquidity. Do not reject unknown liquidity; rely on competition/book checks later.
        if market.liquidity > 0 and market.liquidity < filters.min_liquidity_usdc:
            return False, "low_liquidity"
        if market.volume_24h > 0 and market.volume_24h < filters.min_volume_24h_usdc:
            return False, "low_volume"
        if market.end_date:
            hours = (market.end_date - datetime.now(timezone.utc)).total_seconds() / 3600
            days = hours / 24
            if days < filters.min_days_to_resolution:
                return False, "resolves_too_soon"
            if hours < filters.min_hours_to_resolution or hours > filters.max_hours_to_resolution:
                return False, "outside_resolution_window"
        else:
            # Do not make discovery impossible if a reward endpoint omits end date; strategy will still be conservative.
            logger.debug("{} missing end date", market.condition_id)
        q = market.question.lower()
        if any(keyword.lower() in q for keyword in filters.blocked_keywords):
            return False, "blocked_keyword"
        if any(keyword.lower() in q for keyword in filters.avoid_keywords):
            return False, "avoid_keyword"
        if filters.allowed_topics and (market.category or "").lower() not in {x.lower() for x in filters.allowed_topics}:
            return False, "topic_not_allowed"
        if not filters.allow_sports and (market.category or "").lower() in {"sports", "nba", "nfl", "mlb", "nhl"}:
            return False, "sports_blocked"
        return True, "ok"

from polymarket_lp_bot.config import ApiConfig, MarketFilters
from polymarket_lp_bot.data.data_fetcher import PolymarketDataFetcher


def test_parse_clob_rewards_market_shape_and_filters_without_liquidity():
    fetcher = PolymarketDataFetcher(ApiConfig())
    raw = {
        "condition_id": "0xabc",
        "question": "Will the Fed cut rates in July?",
        "market_slug": "fed-cut-july",
        "rewards_max_spread": 3,
        "rewards_min_size": 5,
        "total_daily_rate": 25,
        "volume_24hr": 500,
        "end_date": "2026-07-10 00:00:00",
        "tokens": [
            {"token_id": "yes-token", "outcome": "YES", "price": 0.5},
            {"token_id": "no-token", "outcome": "NO", "price": 0.5},
        ],
    }
    market = fetcher._parse_market(raw, source="clob_rewards")
    assert market is not None
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"
    assert market.rewards_daily_rate == 25
    assert market.min_incentive_size == 5
    assert market.max_incentive_spread == 0.03
    ok, reason = fetcher._passes_filters(market, MarketFilters(min_daily_rewards_usdc=10, min_volume_24h_usdc=250, min_liquidity_usdc=1000))
    assert (ok, reason) == (True, "ok")

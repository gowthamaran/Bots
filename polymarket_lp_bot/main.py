"""Entrypoint for the Polymarket Liquidity Provider rewards bot."""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress

from loguru import logger

from polymarket_lp_bot.config import load_config
from polymarket_lp_bot.data.data_fetcher import PolymarketDataFetcher
from polymarket_lp_bot.execution import build_executor
from polymarket_lp_bot.monitoring import TelegramNotifier
from polymarket_lp_bot.risk import CircuitBreaker, RiskManager
from polymarket_lp_bot.strategy import LiquidityRewardsStrategy


class LpBot:
    def __init__(self, config_path: str):
        self.cfg = load_config(config_path)
        self.fetcher = PolymarketDataFetcher(self.cfg.api)
        self.risk = RiskManager(self.cfg.risk)
        self.strategy = LiquidityRewardsStrategy(self.cfg.strategy, self.risk)
        self.executor = build_executor(self.cfg)
        self.telegram = TelegramNotifier(self.cfg.telegram)
        self.breaker = CircuitBreaker(self.cfg.risk.circuit_breaker_consecutive_errors)
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self.telegram.start()
        while not self._stop.is_set() and not self.breaker.tripped:
            try:
                await self.run_once()
                self.breaker.record_success()
            except Exception as exc:  # noqa: BLE001 - top-level circuit breaker
                logger.exception("cycle failed")
                self.breaker.record_error()
                await self.telegram.send(f"⚠️ LP cycle failed: {exc}")
            await asyncio.wait([asyncio.create_task(self._stop.wait())], timeout=self.cfg.strategy.loop_interval_seconds)
        if self.breaker.tripped:
            await self.telegram.send("🛑 Circuit breaker tripped; bot paused")
        await self.fetcher.close()

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> None:
        markets = []
        if self.cfg.strategy.auto_discover:
            markets.extend(await self.fetcher.discover_reward_markets(self.cfg.filters))
        markets.extend(await self.fetcher.fetch_configured_markets(self.cfg.strategy.markets))
        # De-duplicate configured + discovered markets.
        markets_by_id = {market.condition_id: market for market in markets}
        logger.info("evaluating {} markets", len(markets_by_id))

        for market in markets_by_id.values():
            snapshot = await self.fetcher.fetch_snapshot(market)
            if self.cfg.strategy.cancel_stale_orders:
                await self.executor.cancel_stale_orders(market.condition_id)
            intents = self.strategy.build_intents(snapshot)
            reports = await self.executor.place_orders(intents)
            await self.telegram.notify_reports(reports)


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Liquidity Rewards LP bot")
    parser.add_argument("--config", default="config/config.yaml", help="Path to YAML config")
    args = parser.parse_args()
    bot = LpBot(args.config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, bot.stop)
    await bot.start()


def cli() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    cli()

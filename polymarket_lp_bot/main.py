"""Entrypoint for the Telegram-accessible Polymarket LP/scalping bot."""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress

from loguru import logger

from polymarket_lp_bot.config import load_config
from polymarket_lp_bot.data.data_fetcher import Market, PolymarketDataFetcher
from polymarket_lp_bot.execution import build_executor
from polymarket_lp_bot.learning import LearningStore
from polymarket_lp_bot.monitoring import TelegramNotifier
from polymarket_lp_bot.risk import CircuitBreaker, RiskManager
from polymarket_lp_bot.strategy import LiquidityRewardsStrategy


class LpBot:
    def __init__(self, config_path: str):
        self.cfg = load_config(config_path)
        self.learning = LearningStore(self.cfg.learning.path, enabled=self.cfg.learning.enabled)
        self.fetcher = PolymarketDataFetcher(self.cfg.api)
        self.risk = RiskManager(self.cfg.risk, self.cfg.policy)
        self.strategy = LiquidityRewardsStrategy(
            self.cfg.strategy,
            self.risk,
            filters=self.cfg.filters,
            policy=self.cfg.policy,
            learning=self.learning,
        )
        self.executor = build_executor(self.cfg)
        self.telegram = TelegramNotifier(self.cfg.telegram)
        self.breaker = CircuitBreaker(self.cfg.risk.circuit_breaker_consecutive_errors)
        self._stop = asyncio.Event()
        self.paused = False
        self._supervisor_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.telegram.start(controller=self)
        self._supervisor_task = asyncio.create_task(self._order_position_supervisor())
        while not self._stop.is_set() and not self.breaker.tripped:
            try:
                if not self.paused:
                    await self.run_once()
                self.breaker.record_success()
            except Exception as exc:  # noqa: BLE001 - top-level circuit breaker
                logger.exception("cycle failed")
                self.breaker.record_error()
                await self.telegram.send(f"⚠️ LP cycle failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.strategy.loop_interval_seconds)
            except asyncio.TimeoutError:
                pass
        if self.breaker.tripped:
            await self.telegram.send("🛑 Circuit breaker tripped; bot paused")
        await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        if self._supervisor_task:
            self._supervisor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._supervisor_task
        await self.telegram.stop()
        await self.fetcher.close()

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> None:
        if not self.risk.trading_window_open():
            await self.executor.cancel_stale_orders(None)
            await self.flatten_positions("overnight_window")
            return
        markets = await self._load_markets()
        logger.info("evaluating {} markets", len(markets))

        for market in markets:
            snapshot = await self.fetcher.fetch_snapshot(market)
            if self.cfg.strategy.cancel_stale_orders:
                await self.executor.cancel_stale_orders(market.condition_id)
            await self.executor.reconcile_market(market.condition_id)
            intents = self.strategy.build_intents(snapshot)
            reports = await self.executor.place_orders(intents)
            if reports:
                self.learning.record(
                    "orders_attempted",
                    market.condition_id,
                    count=len(reports),
                    estimated_reward=sum((r.intent.estimated_daily_reward for r in reports if r.intent), 0.0),
                )
            await self.telegram.notify_reports(reports)

    async def flatten_positions(self, reason: str) -> str:
        await self.executor.cancel_stale_orders(None)
        positions = await self.executor.get_positions()
        reports = await self.executor.sell_positions_immediately(positions, self.cfg.policy.marketable_exit_edge_cents)
        for pos, report in zip(positions, reports, strict=False):
            self.learning.record("exit_attempted", pos.condition_id, failed="error" in report.status)
        await self.telegram.notify_reports(reports)
        return f"Flatten requested ({reason}). Cancelled resting orders and sent {len(reports)} exit orders."

    async def _load_markets(self) -> list[Market]:
        markets: list[Market] = []
        if self.cfg.strategy.auto_discover:
            markets.extend(await self.fetcher.discover_reward_markets(self.cfg.filters))
        markets.extend(await self.fetcher.fetch_configured_markets(self.cfg.strategy.markets))
        markets_by_id = {market.condition_id: market for market in markets}
        return list(markets_by_id.values())

    async def _order_position_supervisor(self) -> None:
        """Continuously parse current orders and immediately sell filled inventory."""

        while not self._stop.is_set():
            try:
                open_orders = await self.executor.get_open_orders(None)
                positions = await self.executor.get_positions()
                if not self.risk.trading_window_open() and open_orders:
                    await self.executor.cancel_stale_orders(None)
                if self.cfg.policy.flatten_positions_immediately and positions:
                    await self.flatten_positions("position_supervisor")
                self.learning.record("supervisor_parse", None, open_orders=len(open_orders), positions=len(positions))
            except Exception as exc:  # noqa: BLE001
                logger.exception("order/position supervisor failed")
                await self.telegram.send(f"⚠️ Supervisor failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.policy.position_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # Telegram controller methods.
    async def telegram_status(self) -> str:
        return (
            f"<b>Status</b>\nmode={self.cfg.strategy.mode}\npaused={self.paused}\n"
            f"trading_window_open={self.risk.trading_window_open()}\n"
            f"never_leave_orders_overnight={self.cfg.policy.never_leave_orders_overnight}\n"
            f"flatten_positions_immediately={self.cfg.policy.flatten_positions_immediately}\n"
            f"min_days_to_resolution={self.cfg.filters.min_days_to_resolution}"
        )

    async def telegram_pause(self) -> str:
        self.paused = True
        await self.executor.cancel_stale_orders(None)
        return "Paused new orders and cancelled resting orders."

    async def telegram_resume(self) -> str:
        self.paused = False
        return "Resumed new order placement."

    async def telegram_run_once(self) -> str:
        await self.run_once()
        return "Completed one scan/quote cycle."

    async def telegram_search(self) -> str:
        markets = await self._load_markets()
        if not markets:
            return "No markets passed filters."
        lines = ["<b>Markets passing filters</b>"]
        for market in markets[:15]:
            lines.append(f"• {market.question[:80]} | rewards=${market.rewards_daily_rate:.0f}/day | liq=${market.liquidity:.0f}")
        return "\n".join(lines)

    async def telegram_orders(self) -> str:
        orders = await self.executor.get_open_orders(None)
        positions = await self.executor.get_positions()
        await self.telegram.notify_orders_positions(orders, positions)
        return f"Parsed {len(orders)} open orders and {len(positions)} positions."

    async def telegram_sellall(self) -> str:
        return await self.flatten_positions("telegram_sellall")

    async def telegram_learning(self) -> str:
        summary = self.learning.summary()
        if not summary:
            return "No learning events yet."
        lines = ["<b>Learning summary</b>"]
        for market_id, stats in list(summary.items())[:10]:
            lines.append(
                f"• {market_id}: cycles={stats['cycles_seen']} fill={stats['fill_rate']:.2%} "
                f"exit_fail={stats['exit_failure_rate']:.2%} comp_q={stats['avg_competition_q']:.1f}"
            )
        return "\n".join(lines)


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

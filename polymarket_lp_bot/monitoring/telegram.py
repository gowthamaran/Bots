"""Telegram notifications and command extension point."""

from __future__ import annotations

from loguru import logger

from polymarket_lp_bot.config import TelegramConfig
from polymarket_lp_bot.execution import ExecutionReport


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self._bot = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        from telegram import Bot

        if self.config.token is None or self.config.chat_id is None:
            logger.warning("Telegram enabled but token/chat_id missing")
            return
        self._bot = Bot(self.config.token.get_secret_value())
        await self.send("🤖 Polymarket LP bot started")

    async def send(self, text: str) -> None:
        if not self._bot or not self.config.chat_id:
            return
        await self._bot.send_message(chat_id=self.config.chat_id, text=text, parse_mode="HTML")

    async def notify_reports(self, reports: list[ExecutionReport]) -> None:
        if not reports:
            return
        lines = ["<b>LP cycle orders</b>"]
        for report in reports:
            i = report.intent
            lines.append(
                f"{report.status}: {i.condition_id} {i.side} {i.price:.2f} x {i.size:.1f} "
                f"yield={i.estimated_daily_yield_pct:.2f}%"
            )
        await self.send("\n".join(lines))

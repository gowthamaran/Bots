"""Telegram command surface and rich alerts.

The user plans to operate the bot through Telegram, so this module exposes both notifications and a
small command console. Trading commands are restricted to the configured `TELEGRAM_CHAT_ID`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from polymarket_lp_bot.config import TelegramConfig
from polymarket_lp_bot.execution import ExecutionReport, OpenOrder, Position


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self._bot = None
        self._application = None
        self._controller: Any = None

    async def start(self, controller: Any | None = None) -> None:
        if not self.config.enabled:
            return
        from telegram import Bot
        from telegram.ext import Application, CommandHandler

        if self.config.token is None or self.config.chat_id is None:
            logger.warning("Telegram enabled but token/chat_id missing")
            return
        token = self.config.token.get_secret_value()
        self._controller = controller
        self._bot = Bot(token)
        self._application = Application.builder().token(token).build()
        for name, handler in {
            "start": self._cmd_start,
            "help": self._cmd_start,
            "status": self._cmd_status,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "run_once": self._cmd_run_once,
            "search": self._cmd_search,
            "orders": self._cmd_orders,
            "sellall": self._cmd_sellall,
            "learning": self._cmd_learning,
        }.items():
            self._application.add_handler(CommandHandler(name, handler))
        await self._application.initialize()
        await self._application.start()
        await self._application.updater.start_polling(poll_interval=self.config.command_poll_seconds)
        await self.send("🤖 <b>Polymarket LP bot online</b>\nUse /help for commands.")

    async def stop(self) -> None:
        if not self._application:
            return
        await self._application.updater.stop()
        await self._application.stop()
        await self._application.shutdown()

    async def send(self, text: str) -> None:
        if not self._bot or not self.config.chat_id:
            return
        await self._bot.send_message(chat_id=self.config.chat_id, text=text, parse_mode="HTML")

    async def notify_reports(self, reports: list[ExecutionReport]) -> None:
        if not reports:
            return
        lines = ["<b>LP cycle orders</b>"]
        for report in reports:
            if report.intent is None:
                lines.append(f"{report.status}: {report.message[:120]}")
                continue
            i = report.intent
            lines.append(
                f"{report.status}: {i.condition_id} {i.side} {i.price:.2f} x {i.size:.1f} "
                f"yield={i.estimated_daily_yield_pct:.2f}%"
            )
        await self.send("\n".join(lines))

    async def notify_orders_positions(self, orders: list[OpenOrder], positions: list[Position]) -> None:
        lines = [f"<b>Open orders:</b> {len(orders)}", f"<b>Positions:</b> {len(positions)}"]
        for order in orders[:10]:
            lines.append(f"• {order.order_id} {order.side} {order.price:.2f} x {order.size:.1f}")
        for pos in positions[:10]:
            lines.append(f"• POS {pos.token_id} {pos.size:.1f} avg={pos.avg_price}")
        await self.send("\n".join(lines))

    def _authorized(self, update: Any) -> bool:
        if not self.config.chat_id:
            return False
        return str(update.effective_chat.id) == str(self.config.chat_id)

    async def _guard(self, update: Any, action: Callable[[], Awaitable[str]]) -> None:
        if not self._authorized(update):
            return
        text = await action()
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_start(self, update: Any, context: Any) -> None:
        await self._guard(update, lambda: self._static_help())

    async def _static_help(self) -> str:
        return (
            "<b>Commands</b>\n"
            "/status - bot mode, pause state, policy\n"
            "/search - discover low-competition reward markets\n"
            "/run_once - execute one scan/quote cycle\n"
            "/orders - parse current open orders and positions\n"
            "/sellall - cancel orders and immediately sell/flatten positions\n"
            "/pause - stop placing new orders\n"
            "/resume - allow new orders\n"
            "/learning - show adaptive memory summary"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_status)

    async def _cmd_pause(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_pause)

    async def _cmd_resume(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_resume)

    async def _cmd_run_once(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_run_once)

    async def _cmd_search(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_search)

    async def _cmd_orders(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_orders)

    async def _cmd_sellall(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_sellall)

    async def _cmd_learning(self, update: Any, context: Any) -> None:
        await self._guard(update, self._controller.telegram_learning)

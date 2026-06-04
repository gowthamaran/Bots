"""Paper/live execution adapters.

Live mode uses Polymarket's official py-clob-client-v2 package through a small adapter. The import is
performed only when live execution is requested so tests and paper mode do not require credentials.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from loguru import logger

from polymarket_lp_bot.config import ApiConfig, BotConfig
from polymarket_lp_bot.scoring import Side
from polymarket_lp_bot.strategy import OrderIntent


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    intent: OrderIntent
    status: str
    order_id: str | None = None
    message: str = ""


class BaseExecutor(ABC):
    @abstractmethod
    async def reconcile_market(self, condition_id: str) -> None: ...

    @abstractmethod
    async def place_orders(self, intents: list[OrderIntent]) -> list[ExecutionReport]: ...

    @abstractmethod
    async def cancel_stale_orders(self, condition_id: str) -> None: ...


class PaperExecutor(BaseExecutor):
    async def reconcile_market(self, condition_id: str) -> None:
        logger.info("paper reconcile {}", condition_id)

    async def place_orders(self, intents: list[OrderIntent]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        for intent in intents:
            logger.info(
                "PAPER {} {} {} @ {:.2f} x {:.2f}; est_reward=${:.2f}/day yield={:.3f}% ({})",
                intent.condition_id,
                intent.side,
                intent.outcome,
                intent.price,
                intent.size,
                intent.estimated_daily_reward,
                intent.estimated_daily_yield_pct,
                intent.reason,
            )
            reports.append(ExecutionReport(intent=intent, status="paper"))
        return reports

    async def cancel_stale_orders(self, condition_id: str) -> None:
        logger.info("paper cancel stale {}", condition_id)


class ClobExecutor(BaseExecutor):
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.client = self._build_client(cfg.api)

    def _build_client(self, api: ApiConfig) -> Any:
        clob_module = importlib.import_module("py_clob_client.client")
        ClobClient = getattr(clob_module, "ClobClient")
        key = self.cfg.secrets.polymarket_private_key
        assert key is not None
        kwargs: dict[str, Any] = {"host": api.clob_base_url, "key": key.get_secret_value(), "chain_id": api.chain_id}
        if self.cfg.secrets.polymarket_signature_type is not None:
            kwargs["signature_type"] = self.cfg.secrets.polymarket_signature_type
        if self.cfg.secrets.polymarket_funder:
            kwargs["funder"] = self.cfg.secrets.polymarket_funder
        client = ClobClient(**kwargs)
        if hasattr(client, "set_api_creds") and hasattr(client, "create_or_derive_api_creds"):
            client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def reconcile_market(self, condition_id: str) -> None:
        logger.info("live reconcile {}", condition_id)

    async def cancel_stale_orders(self, condition_id: str) -> None:
        # py-clob-client versions expose different helpers; keep this adapter narrow and auditable.
        logger.info("live stale-order cancellation requested for {}", condition_id)

    async def place_orders(self, intents: list[OrderIntent]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        for intent in intents:
            if intent.size <= 0:
                reports.append(ExecutionReport(intent, status="rejected", message="non-positive size"))
                continue
            try:
                response = await self._place_one(intent)
                order_id = response.get("orderID") or response.get("order_id") if isinstance(response, dict) else None
                reports.append(ExecutionReport(intent, status="live", order_id=order_id, message=str(response)))
            except Exception as exc:  # noqa: BLE001 - report and continue per-order
                logger.exception("live order failed without exposing secrets")
                reports.append(ExecutionReport(intent, status="error", message=str(exc)))
        return reports

    async def _place_one(self, intent: OrderIntent) -> Any:
        order_args_module = importlib.import_module("py_clob_client.clob_types")
        OrderArgs = getattr(order_args_module, "OrderArgs")
        order_type_module = importlib.import_module("py_clob_client.order_builder.constants")
        BUY = getattr(order_type_module, "BUY", "BUY")
        SELL = getattr(order_type_module, "SELL", "SELL")
        side = BUY if intent.side == Side.BID else SELL
        order_args = OrderArgs(price=intent.price, size=intent.size, side=side, token_id=intent.token_id)
        signed = self.client.create_order(order_args)
        return self.client.post_order(signed)


def build_executor(cfg: BotConfig) -> BaseExecutor:
    if cfg.strategy.mode == "live":
        return ClobExecutor(cfg)
    return PaperExecutor()

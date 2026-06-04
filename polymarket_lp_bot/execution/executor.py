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
    intent: OrderIntent | None
    status: str
    order_id: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class OpenOrder:
    order_id: str
    condition_id: str | None
    token_id: str
    side: str
    price: float
    size: float
    filled_size: float = 0.0


@dataclass(frozen=True, slots=True)
class Position:
    condition_id: str | None
    token_id: str
    outcome: str | None
    size: float
    avg_price: float | None = None


class BaseExecutor(ABC):
    @abstractmethod
    async def reconcile_market(self, condition_id: str) -> None: ...

    @abstractmethod
    async def place_orders(self, intents: list[OrderIntent]) -> list[ExecutionReport]: ...

    @abstractmethod
    async def cancel_stale_orders(self, condition_id: str | None = None) -> None: ...

    @abstractmethod
    async def get_open_orders(self, condition_id: str | None = None) -> list[OpenOrder]: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def sell_positions_immediately(self, positions: list[Position], edge_cents: float) -> list[ExecutionReport]: ...


class PaperExecutor(BaseExecutor):
    def __init__(self) -> None:
        self.open_orders: list[OpenOrder] = []
        self.positions: list[Position] = []
        self._order_seq = 0

    async def reconcile_market(self, condition_id: str) -> None:
        logger.info("paper reconcile {}", condition_id)

    async def place_orders(self, intents: list[OrderIntent]) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        for intent in intents:
            self._order_seq += 1
            order_id = f"paper-{self._order_seq}"
            self.open_orders.append(
                OpenOrder(order_id, intent.condition_id, intent.token_id, intent.side.value, intent.price, intent.size)
            )
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
            reports.append(ExecutionReport(intent=intent, status="paper", order_id=order_id))
        return reports

    async def cancel_stale_orders(self, condition_id: str | None = None) -> None:
        before = len(self.open_orders)
        self.open_orders = [o for o in self.open_orders if condition_id is not None and o.condition_id != condition_id]
        logger.info("paper cancelled {} stale orders for {}", before - len(self.open_orders), condition_id or "ALL")

    async def get_open_orders(self, condition_id: str | None = None) -> list[OpenOrder]:
        return [o for o in self.open_orders if condition_id is None or o.condition_id == condition_id]

    async def get_positions(self) -> list[Position]:
        return list(self.positions)

    async def sell_positions_immediately(self, positions: list[Position], edge_cents: float) -> list[ExecutionReport]:
        reports = [ExecutionReport(None, "paper_exit", message=f"would sell {p.size} of {p.token_id}") for p in positions]
        self.positions = []
        return reports


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
        orders = await self.get_open_orders(condition_id)
        logger.info("{} live open orders for {}", len(orders), condition_id)

    async def cancel_stale_orders(self, condition_id: str | None = None) -> None:
        orders = await self.get_open_orders(condition_id)
        for order in orders:
            try:
                if hasattr(self.client, "cancel"):
                    self.client.cancel(order.order_id)
                elif hasattr(self.client, "cancel_order"):
                    self.client.cancel_order(order.order_id)
                else:
                    logger.warning("py-clob-client has no recognized cancel method")
                    return
            except Exception:
                logger.exception("failed to cancel order {}", order.order_id)

    async def get_open_orders(self, condition_id: str | None = None) -> list[OpenOrder]:
        raw = self._call_first_available(["get_orders", "get_open_orders"], {"market": condition_id} if condition_id else {})
        items = raw if isinstance(raw, list) else raw.get("orders", raw.get("data", [])) if isinstance(raw, dict) else []
        return [order for item in items if (order := self._parse_open_order(item, condition_id)) is not None]

    async def get_positions(self) -> list[Position]:
        raw = self._call_first_available(["get_positions", "get_balance_allowance"], {})
        items = raw if isinstance(raw, list) else raw.get("positions", raw.get("data", [])) if isinstance(raw, dict) else []
        return [pos for item in items if (pos := self._parse_position(item)) is not None and pos.size > 0]

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

    async def sell_positions_immediately(self, positions: list[Position], edge_cents: float) -> list[ExecutionReport]:
        """Flatten inventory with aggressive sell limits.

        This keeps the user's requested policy explicit: fills are treated as inventory to exit, not a
        directional bet to hold. The adapter uses a conservative price fallback when no bid is known;
        production deployments should wire a fresh book lookup before calling this method.
        """

        reports: list[ExecutionReport] = []
        for position in positions:
            try:
                order_args_module = importlib.import_module("py_clob_client.clob_types")
                OrderArgs = getattr(order_args_module, "OrderArgs")
                constants = importlib.import_module("py_clob_client.order_builder.constants")
                SELL = getattr(constants, "SELL", "SELL")
                price = max(0.01, min(0.99, (position.avg_price or 0.5) - edge_cents / 100.0))
                order_args = OrderArgs(price=round(price, 2), size=position.size, side=SELL, token_id=position.token_id)
                signed = self.client.create_order(order_args)
                response = self.client.post_order(signed)
                reports.append(ExecutionReport(None, "exit_live", message=str(response)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("immediate exit failed")
                reports.append(ExecutionReport(None, "exit_error", message=str(exc)))
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

    def _call_first_available(self, method_names: list[str], params: dict[str, Any]) -> Any:
        for name in method_names:
            method = getattr(self.client, name, None)
            if method is None:
                continue
            try:
                return method(**params) if params else method()
            except TypeError:
                return method()
        return []

    @staticmethod
    def _parse_open_order(item: dict[str, Any], condition_hint: str | None) -> OpenOrder | None:
        try:
            return OpenOrder(
                order_id=str(item.get("id") or item.get("orderID") or item.get("order_id")),
                condition_id=item.get("condition_id") or item.get("conditionId") or item.get("market") or condition_hint,
                token_id=str(item.get("token_id") or item.get("asset_id") or item.get("assetId")),
                side=str(item.get("side")),
                price=float(item.get("price")),
                size=float(item.get("size") or item.get("original_size") or 0),
                filled_size=float(item.get("filled_size") or item.get("filledSize") or 0),
            )
        except Exception:
            return None

    @staticmethod
    def _parse_position(item: dict[str, Any]) -> Position | None:
        try:
            return Position(
                condition_id=item.get("condition_id") or item.get("conditionId") or item.get("market"),
                token_id=str(item.get("token_id") or item.get("asset_id") or item.get("assetId")),
                outcome=item.get("outcome"),
                size=float(item.get("size") or item.get("balance") or 0),
                avg_price=float(item.get("avg_price") or item.get("average_price")) if item.get("avg_price") or item.get("average_price") else None,
            )
        except Exception:
            return None


def build_executor(cfg: BotConfig) -> BaseExecutor:
    if cfg.strategy.mode == "live":
        return ClobExecutor(cfg)
    return PaperExecutor()

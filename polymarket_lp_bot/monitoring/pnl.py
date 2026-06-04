"""Reward-vs-trading PnL event tracker.

This lightweight tracker is deployable without a database. It records fills, exits, reward credits,
and daily expectation snapshots to JSONL so Telegram can report whether LP rewards are actually
beating adverse-selection losses.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PnlEvent:
    event_type: str
    amount_usdc: float = 0.0
    market_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True, slots=True)
class PnlSummary:
    expected_rewards: float
    realized_rewards: float
    realized_trading_pnl: float
    exit_slippage: float
    net_profit: float
    events: int


class PnlTracker:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, amount_usdc: float = 0.0, market_id: str | None = None, **payload: Any) -> None:
        event = PnlEvent(event_type=event_type, amount_usdc=amount_usdc, market_id=market_id, payload=payload)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), default=str, sort_keys=True) + "\n")

    def summarize(self, day: date | None = None) -> PnlSummary:
        day = day or datetime.now(timezone.utc).date()
        expected = rewards = trading = slippage = 0.0
        count = 0
        for event in self._events_for_day(day):
            count += 1
            if event.event_type == "expected_reward":
                expected += event.amount_usdc
            elif event.event_type == "reward_received":
                rewards += event.amount_usdc
            elif event.event_type == "trade_pnl":
                trading += event.amount_usdc
            elif event.event_type == "exit_slippage":
                slippage += event.amount_usdc
        return PnlSummary(expected, rewards, trading, slippage, rewards + trading - abs(slippage), count)

    def _events_for_day(self, day: date) -> list[PnlEvent]:
        if not self.path.exists():
            return []
        events: list[PnlEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
                ts = datetime.fromisoformat(raw["ts"])
                if ts.date() == day:
                    events.append(PnlEvent(**raw))
            except Exception:
                continue
        return events

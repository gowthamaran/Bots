"""Persistent step-by-step learning memory for strategy adaptation.

The bot is deliberately conservative: it does not train an opaque ML model in v1. Instead it records
structured observations for every market/cycle/order decision and derives transparent adaptive
penalties from recent outcomes. This lets the bot "learn from every step" while keeping trading
behavior auditable and easy to override from YAML or Telegram.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LearningEvent:
    event_type: str
    market_id: str | None
    payload: dict[str, Any]
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(slots=True)
class MarketMemory:
    cycles_seen: int = 0
    orders_attempted: int = 0
    orders_filled: int = 0
    exits_attempted: int = 0
    exit_failures: int = 0
    cumulative_estimated_reward: float = 0.0
    recent_competition_q: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    recent_yield_pct: deque[float] = field(default_factory=lambda: deque(maxlen=100))

    @property
    def fill_rate(self) -> float:
        return self.orders_filled / self.orders_attempted if self.orders_attempted else 0.0

    @property
    def exit_failure_rate(self) -> float:
        return self.exit_failures / self.exits_attempted if self.exits_attempted else 0.0

    @property
    def avg_competition_q(self) -> float:
        return sum(self.recent_competition_q) / len(self.recent_competition_q) if self.recent_competition_q else 0.0

    @property
    def avg_yield_pct(self) -> float:
        return sum(self.recent_yield_pct) / len(self.recent_yield_pct) if self.recent_yield_pct else 0.0


class LearningStore:
    """Append-only JSONL event log plus in-memory market summaries."""

    def __init__(self, path: str | Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self.markets: dict[str, MarketMemory] = defaultdict(MarketMemory)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._replay_existing()

    def record(self, event_type: str, market_id: str | None = None, **payload: Any) -> None:
        if not self.enabled:
            return
        event = LearningEvent(event_type=event_type, market_id=market_id, payload=payload)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), default=str, sort_keys=True) + "\n")
        self._apply(event)

    def market_penalty_bps(self, market_id: str) -> float:
        """Return an adaptive yield hurdle penalty in percentage points.

        Markets where exits have been unreliable or competition has crept up require higher expected
        yield before the strategy will quote again.
        """

        memory = self.markets[market_id]
        penalty = 0.0
        if memory.exit_failure_rate > 0.05:
            penalty += min(1.0, memory.exit_failure_rate * 5.0)
        if memory.avg_competition_q > 0:
            penalty += min(0.5, memory.avg_competition_q / 10_000.0)
        return penalty

    def summary(self) -> dict[str, Any]:
        return {
            market_id: {
                "cycles_seen": mem.cycles_seen,
                "orders_attempted": mem.orders_attempted,
                "orders_filled": mem.orders_filled,
                "fill_rate": mem.fill_rate,
                "exit_failure_rate": mem.exit_failure_rate,
                "avg_competition_q": mem.avg_competition_q,
                "avg_yield_pct": mem.avg_yield_pct,
            }
            for market_id, mem in self.markets.items()
        }

    def _replay_existing(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                self._apply(LearningEvent(**raw))
            except Exception:
                # Corrupt historical events should not stop the trading process.
                continue

    def _apply(self, event: LearningEvent) -> None:
        if not event.market_id:
            return
        mem = self.markets[event.market_id]
        if event.event_type == "cycle_evaluated":
            mem.cycles_seen += 1
            if "competition_q_min" in event.payload:
                mem.recent_competition_q.append(float(event.payload["competition_q_min"]))
            if "estimated_yield_pct" in event.payload:
                mem.recent_yield_pct.append(float(event.payload["estimated_yield_pct"]))
        elif event.event_type == "orders_attempted":
            mem.orders_attempted += int(event.payload.get("count", 0))
            mem.cumulative_estimated_reward += float(event.payload.get("estimated_reward", 0.0))
        elif event.event_type == "orders_filled":
            mem.orders_filled += int(event.payload.get("count", 0))
        elif event.event_type == "exit_attempted":
            mem.exits_attempted += int(event.payload.get("count", 1))
            if event.payload.get("failed"):
                mem.exit_failures += 1

"""Backtesting harness stub for historical reward-share simulations.

The production bot records enough JSONL events to replay market-selection, quote placement, and
exit decisions. This harness is intentionally minimal but deployable as an extension point: feed it
historical snapshots and it returns expected reward/PnL metrics using the same strategy objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class SnapshotLike(Protocol):
    condition_id: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    snapshots: int
    expected_reward_usdc: float
    simulated_trading_pnl_usdc: float
    net_usdc: float


class BacktestHarness:
    def run(self, snapshots: Iterable[SnapshotLike]) -> BacktestResult:
        count = sum(1 for _ in snapshots)
        # TODO: wire historical books to OpportunityAnalyzer + strategy. Keeping the interface stable
        # lets deployers plug in parquet/CSV snapshots without changing the live bot.
        return BacktestResult(snapshots=count, expected_reward_usdc=0.0, simulated_trading_pnl_usdc=0.0, net_usdc=0.0)

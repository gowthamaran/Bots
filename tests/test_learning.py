from polymarket_lp_bot.learning import LearningStore


def test_learning_store_records_and_penalizes_exit_failures(tmp_path):
    store = LearningStore(tmp_path / "events.jsonl", enabled=True)
    store.record("cycle_evaluated", "m1", competition_q_min=100, estimated_yield_pct=0.4)
    store.record("orders_attempted", "m1", count=2, estimated_reward=1.2)
    store.record("exit_attempted", "m1", failed=True)
    summary = store.summary()["m1"]
    assert summary["cycles_seen"] == 1
    assert summary["orders_attempted"] == 2
    assert summary["exit_failure_rate"] == 1.0
    assert store.market_penalty_bps("m1") > 0

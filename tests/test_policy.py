from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from polymarket_lp_bot.config import RiskConfig, TradingPolicyConfig
from polymarket_lp_bot.risk import RiskManager


def test_no_overnight_window_blocks_after_cutoff_and_before_resume():
    risk = RiskManager(RiskConfig(), TradingPolicyConfig(overnight_cancel_after_utc="23:30", resume_trading_after_utc="00:10"))
    assert risk.trading_window_open(datetime(2026, 6, 4, 23, 29, tzinfo=timezone.utc)) is True
    assert risk.trading_window_open(datetime(2026, 6, 4, 23, 30, tzinfo=timezone.utc)) is False
    assert risk.trading_window_open(datetime(2026, 6, 5, 0, 5, tzinfo=timezone.utc)) is False
    assert risk.trading_window_open(datetime(2026, 6, 5, 0, 10, tzinfo=timezone.utc)) is True


def test_resolution_policy_requires_more_than_seven_days():
    risk = RiskManager(RiskConfig())
    near = SimpleNamespace(end_date=datetime.now(timezone.utc) + timedelta(days=6, hours=23))
    far = SimpleNamespace(end_date=datetime.now(timezone.utc) + timedelta(days=8))
    assert risk.resolution_allowed(near, min_days=7) is False
    assert risk.resolution_allowed(far, min_days=7) is True

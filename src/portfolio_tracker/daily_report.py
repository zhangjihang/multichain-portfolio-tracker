"""Daily and weekly report generation (plain text for Discord webhook posting)."""

from __future__ import annotations

import time

from .adapters.jupiter_jlp import get_jlp_weights
from .report_formatter import format_report
from .service import PortfolioService
from .storage import SnapshotStorage


async def generate_daily_report(service: PortfolioService, storage: SnapshotStorage, health_threshold: float = 1.5) -> str:
    """Generate daily report as formatted text."""
    data = await service.get_portfolio()
    now = time.time()
    prev = await storage.get_nearest(now - 86400, exclude_after=now - 3600)
    jlp_w = await get_jlp_weights()
    return format_report(data, prev_data=prev, report_type="daily", jlp_weights=jlp_w)


async def generate_weekly_report(service: PortfolioService, storage: SnapshotStorage, health_threshold: float = 1.5) -> str:
    """Generate weekly report with week-over-week comparison."""
    data = await service.get_portfolio()
    now = time.time()
    prev = await storage.get_nearest(now - 7 * 86400, exclude_after=now - 3600)
    jlp_w = await get_jlp_weights()
    return format_report(data, prev_data=prev, report_type="weekly", jlp_weights=jlp_w)

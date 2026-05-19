"""Health alert checking (stateless CLI, state managed by caller/cron)."""

from __future__ import annotations

import json
from decimal import Decimal

from .service import PortfolioService


async def check_health_alerts(service: PortfolioService, threshold: float = 1.5) -> list[dict]:
    """Check DeFi positions and return those below health threshold."""
    data = await service.get_portfolio()
    alerts = []
    for pos in data.get("defi_positions", []):
        h = (pos.get("detail") or {}).get("health_rate")
        if h is not None and float(h) < threshold:
            addr = pos.get("address", "")
            alerts.append({
                "protocol": pos.get("protocol"),
                "chain": pos.get("chain"),
                "name": pos.get("name"),
                "health_rate": float(h),
                "threshold": threshold,
                "address": f"{addr[:6]}...{addr[-4:]}" if addr else "",
                "supply": [{"symbol": t.get("symbol"), "value": float(t.get("value", 0))} for t in pos.get("supply", [])],
                "borrow": [{"symbol": t.get("symbol"), "value": float(t.get("value", 0))} for t in pos.get("borrow", [])],
            })
    return alerts

"""SQLite storage for portfolio snapshots."""

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite


def _json_default(obj: Any) -> Any:
    """Serialize Decimal and other objects for JSON storage."""
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


class SnapshotStorage:
    """SQLite-backed snapshot storage."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._initialized = False

    async def _init_db(self) -> None:
        """Ensure database and schema exist."""
        if self._initialized:
            return

        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    total_assets REAL NOT NULL,
                    total_debts REAL NOT NULL,
                    net_worth REAL NOT NULL,
                    data_json TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price_usd REAL,
                    value_usd REAL,
                    notes TEXT,
                    is_active INTEGER DEFAULT 1,
                    expires_at TEXT,
                    reminded INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.commit()

        self._initialized = True

    async def save_snapshot(self, snapshot: dict) -> int:
        """Persist a snapshot and return its row ID."""
        await self._init_db()

        timestamp = float(snapshot.get("timestamp", time.time()))
        total_assets = float(snapshot.get("total_assets", 0))
        total_debts = float(snapshot.get("total_debts", 0))
        net_worth = float(snapshot.get("net_worth", total_assets - total_debts))
        data_json = json.dumps(snapshot, default=_json_default)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO snapshots (timestamp, total_assets, total_debts, net_worth, data_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, total_assets, total_debts, net_worth, data_json),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_latest(self) -> dict | None:
        """Fetch the most recent snapshot."""
        await self._init_db()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, timestamp, total_assets, total_debts, net_worth, data_json
                FROM snapshots
                ORDER BY timestamp DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()

        if not row:
            return None

        return self._row_to_snapshot(row)

    async def get_history(self, days: int) -> list[dict]:
        """Fetch snapshots within the past N days."""
        await self._init_db()

        since = time.time() - days * 86400

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, timestamp, total_assets, total_debts, net_worth, data_json
                FROM snapshots
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (since,),
            )
            rows = await cursor.fetchall()

        return [self._row_to_snapshot(row) for row in rows]

    async def get_nearest(self, target_ts: float, exclude_after: float | None = None) -> dict | None:
        """Fetch the snapshot closest to the given timestamp.

        Args:
            target_ts: target timestamp to find the nearest snapshot to.
            exclude_after: if set, exclude snapshots with timestamp > this value.
                           Useful to avoid comparing a snapshot against itself.
        """
        await self._init_db()

        if exclude_after is not None:
            query = """
                SELECT id, timestamp, total_assets, total_debts, net_worth, data_json
                FROM snapshots
                WHERE timestamp <= ?
                ORDER BY ABS(timestamp - ?) ASC
                LIMIT 1
            """
            params = (exclude_after, target_ts)
        else:
            query = """
                SELECT id, timestamp, total_assets, total_debts, net_worth, data_json
                FROM snapshots
                ORDER BY ABS(timestamp - ?) ASC
                LIMIT 1
            """
            params = (target_ts,)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_snapshot(row)

    @staticmethod
    def _row_to_snapshot(row: aiosqlite.Row) -> dict:
        """Convert a database row into a structured snapshot dict."""
        return {
            "id": row["id"],
            "timestamp": float(row["timestamp"]),
            "total_assets": Decimal(str(row["total_assets"])),
            "total_debts": Decimal(str(row["total_debts"])),
            "net_worth": Decimal(str(row["net_worth"])),
            "data": json.loads(row["data_json"]),
        }

    # ── Manual entries ──────────────────────────────────────────────

    async def add_manual_entry(
        self,
        project: str,
        coin: str,
        quantity: float,
        price_usd: float | None = None,
        notes: str | None = None,
        expires_at: str | None = None,
    ) -> int:
        await self._init_db()
        now = time.time()
        value_usd = price_usd * quantity if price_usd is not None else None
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO manual_entries
                    (project, coin, quantity, price_usd, value_usd, notes, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project, coin, quantity, price_usd, value_usd, notes, expires_at, now, now),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_manual_entries(self, active_only: bool = True) -> list[dict]:
        await self._init_db()
        query = "SELECT * FROM manual_entries"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_manual_entry(self, entry_id: int, **kwargs) -> None:
        await self._init_db()
        allowed = {"project", "coin", "quantity", "price_usd", "value_usd", "notes", "is_active", "expires_at"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [entry_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE manual_entries SET {set_clause} WHERE id = ?", values)
            await db.commit()

    async def remove_manual_entry(self, entry_id: int) -> None:
        await self.update_manual_entry(entry_id, is_active=0)

    async def get_expiring_entries(self, target_date: str | None = None) -> list[dict]:
        await self._init_db()
        if target_date is None:
            target_date = date.today().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM manual_entries
                WHERE expires_at = ? AND reminded = 0 AND is_active = 1
                """,
                (target_date,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_reminded(self, entry_id: int) -> None:
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE manual_entries SET reminded = 1, updated_at = ? WHERE id = ?", (time.time(), entry_id))
            await db.commit()

    async def get_manual_total(self, prices: dict[str, float] | None = None) -> float:
        entries = await self.get_manual_entries(active_only=True)
        total = 0.0
        for e in entries:
            if prices and e["coin"] in prices:
                total += prices[e["coin"]] * e["quantity"]
            elif e["value_usd"] is not None:
                total += e["value_usd"]
        return total
